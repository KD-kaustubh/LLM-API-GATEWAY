from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.api.routes import get_inference_service
from gateway.api.schemas import MAX_CONTENT_CHARS, MAX_MESSAGES, MAX_MODEL_LENGTH, MAX_OUTPUT_TOKENS
from gateway.errors import ProviderError
from gateway.main import app
from gateway.providers.base import ProviderRequest, ProviderResponse
from gateway.services.inference import InferenceService

URL = "/v1/chat/completions"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "mock",
        "messages": [{"role": "user", "content": "Explain REST APIs in simple terms"}],
    }
    payload.update(overrides)
    return payload


def test_mock_completion_succeeds(client: TestClient) -> None:
    response = client.post(URL, json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("req_")
    assert body["object"] == "chat.completion"
    assert body["model"] == "mock"
    assert body["provider"] == "mock"
    assert body["content"] == "Mock response: Explain REST APIs in simple terms"
    assert body["usage"] == {"input_tokens": 6, "output_tokens": 8, "total_tokens": 14}


def test_response_schema_has_exact_fields(client: TestClient) -> None:
    body = client.post(URL, json=_payload()).json()
    assert set(body) == {"id", "object", "model", "provider", "content", "usage"}
    assert set(body["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


def test_request_ids_are_unique(client: TestClient) -> None:
    first = client.post(URL, json=_payload()).json()["id"]
    second = client.post(URL, json=_payload()).json()["id"]
    assert first != second


def test_optional_parameters_accepted(client: TestClient) -> None:
    response = client.post(URL, json=_payload(temperature=0.7, max_tokens=100))
    assert response.status_code == 200


def test_all_roles_accepted(client: TestClient) -> None:
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "Bye"},
    ]
    response = client.post(URL, json=_payload(messages=messages))
    assert response.status_code == 200
    assert response.json()["content"] == "Mock response: Bye"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"messages": []}, id="empty-messages"),
        pytest.param({"messages": [{"role": "tool", "content": "x"}]}, id="invalid-role"),
        pytest.param({"messages": [{"role": "user", "content": ""}]}, id="empty-content"),
        pytest.param({"messages": [{"role": "user", "content": "   "}]}, id="blank-content"),
        pytest.param({"messages": [{"role": "user"}]}, id="missing-content"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"model": "  "}, id="blank-model"),
        pytest.param({"temperature": -0.1}, id="temperature-too-low"),
        pytest.param({"temperature": 2.1}, id="temperature-too-high"),
        pytest.param({"temperature": "hot"}, id="temperature-not-number"),
        pytest.param({"max_tokens": 0}, id="max-tokens-zero"),
        pytest.param({"max_tokens": -5}, id="max-tokens-negative"),
        pytest.param({"max_tokens": "10"}, id="max-tokens-string"),
        pytest.param({"stream": True}, id="unknown-field"),
        pytest.param({"model": "m" * (MAX_MODEL_LENGTH + 1)}, id="model-too-long"),
        pytest.param(
            {"messages": [{"role": "user", "content": "hi"}] * (MAX_MESSAGES + 1)},
            id="too-many-messages",
        ),
        pytest.param(
            {"messages": [{"role": "user", "content": "x" * (MAX_CONTENT_CHARS + 1)}]},
            id="content-too-long",
        ),
        pytest.param({"max_tokens": MAX_OUTPUT_TOKENS + 1}, id="max-tokens-too-large"),
    ],
)
def test_invalid_requests_rejected(client: TestClient, overrides: dict[str, Any]) -> None:
    response = client.post(URL, json=_payload(**overrides))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "invalid_request"
    assert error["details"]


def test_values_at_limits_accepted(client: TestClient) -> None:
    payload = _payload(
        messages=[{"role": "user", "content": "hi"}] * (MAX_MESSAGES - 1)
        + [{"role": "user", "content": "x" * MAX_CONTENT_CHARS}],
        temperature=2.0,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    assert client.post(URL, json=payload).status_code == 200


def test_missing_model_rejected(client: TestClient) -> None:
    response = client.post(URL, json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 422


def test_unsupported_model_returns_400(client: TestClient) -> None:
    response = client.post(URL, json=_payload(model="gpt-unknown"))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "unsupported_model"
    assert "gpt-unknown" in error["message"]


@pytest.mark.parametrize(("model", "env_var"), [("groq", "GROQ_API_KEY"), ("gemini", "GOOGLE_API_KEY")])
def test_missing_credentials_returns_503(client: TestClient, model: str, env_var: str) -> None:
    response = client.post(URL, json=_payload(model=model))

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "provider_not_configured"
    assert env_var in error["message"]


class _FailingProvider:
    name = "failing"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        raise self._exc


def _use_provider(provider: _FailingProvider) -> None:
    app.dependency_overrides[get_inference_service] = lambda: InferenceService(lambda _: provider)


def test_provider_failure_returns_502(client: TestClient) -> None:
    _use_provider(_FailingProvider(ProviderError("Groq request failed with status 500")))

    response = client.post(URL, json=_payload())

    assert response.status_code == 502
    assert response.json() == {
        "error": {"type": "provider_error", "message": "Groq request failed with status 500"}
    }


def test_unexpected_error_hides_internals(client: TestClient) -> None:
    _use_provider(_FailingProvider(RuntimeError("secret-internal-detail at C:\\path\\file.py")))
    lenient_client = TestClient(app, raise_server_exceptions=False, headers=client.headers)

    response = lenient_client.post(URL, json=_payload())

    assert response.status_code == 500
    assert response.json() == {
        "error": {"type": "internal_error", "message": "An unexpected error occurred"}
    }
