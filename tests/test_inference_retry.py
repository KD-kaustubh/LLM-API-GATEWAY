from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.api.routes import get_inference_service
from gateway.api.schemas import ChatCompletionRequest
from gateway.config import Settings
from gateway.errors import (
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    TransientProviderError,
)
from gateway.main import app
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage
from gateway.services.inference import InferenceService
from gateway.services.retry import Retrier, RetryPolicy

URL = "/v1/chat/completions"
PAYLOAD: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hi"}]}


class CountingProvider:
    """Raises scripted errors in order, then succeeds; counts every attempt."""

    name = "counting"

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return ProviderResponse(self.name, "counting-v1", "done", TokenUsage(1, 1, 2))


def _service(provider: CountingProvider, max_retries: int = 2) -> InferenceService:
    retrier = Retrier(RetryPolicy(max_retries=max_retries), sleep=lambda _: None, jitter=lambda: 0.0)
    return InferenceService(lambda _: provider, retrier)


@pytest.fixture
def use_provider(anon_client: TestClient) -> Iterator[Any]:
    """Route the API to a CountingProvider behind a real Retrier with a no-op sleep."""

    def install(provider: CountingProvider, max_retries: int = 2) -> CountingProvider:
        app.dependency_overrides[get_inference_service] = lambda: _service(provider, max_retries)
        return provider

    yield install


# --- service level -----------------------------------------------------------


def test_service_retries_transient_failures_then_returns_normalized_response() -> None:
    provider = CountingProvider(TransientProviderError("a"), ProviderTimeoutError("b"))

    response = _service(provider).create_chat_completion(ChatCompletionRequest.model_validate(PAYLOAD))

    assert provider.calls == 3
    assert response.content == "done"
    assert response.id.startswith("req_")


def test_provider_resolution_errors_are_not_retried() -> None:
    resolutions = 0

    def resolve(model: str) -> CountingProvider:
        nonlocal resolutions
        resolutions += 1
        raise ProviderNotConfiguredError("Provider 'groq' is not configured: set GROQ_API_KEY")

    service = InferenceService(resolve, Retrier(RetryPolicy(max_retries=5), sleep=lambda _: None))

    with pytest.raises(ProviderNotConfiguredError):
        service.create_chat_completion(ChatCompletionRequest.model_validate(PAYLOAD))
    assert resolutions == 1


def test_default_service_does_not_retry() -> None:
    provider = CountingProvider(TransientProviderError("blip"))
    with pytest.raises(TransientProviderError):
        InferenceService(lambda _: provider).create_chat_completion(ChatCompletionRequest.model_validate(PAYLOAD))
    assert provider.calls == 1


def test_inference_service_dependency_uses_configured_policy(settings: Settings) -> None:
    configured = settings.model_copy(update={"max_retries": 4, "retry_base_delay": 0.1, "retry_max_delay": 1.5})
    service = get_inference_service(configured)
    assert service._retrier._policy == RetryPolicy(max_retries=4, base_delay=0.1, max_delay=1.5)


# --- API level ---------------------------------------------------------------


def test_api_success_after_transient_failures(client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider(TransientProviderError("a"), TransientProviderError("b")))

    response = client.post(URL, json=PAYLOAD)

    assert response.status_code == 200
    assert provider.calls == 3


def test_api_exhausted_retries_return_normalized_provider_error(client: TestClient, use_provider: Any) -> None:
    errors = [TransientProviderError("Counting request failed with status 503") for _ in range(5)]
    provider = use_provider(CountingProvider(*errors), max_retries=2)

    response = client.post(URL, json=PAYLOAD)

    assert provider.calls == 3
    assert response.status_code == 502
    assert response.json() == {
        "error": {"type": "provider_error", "message": "Counting request failed with status 503"}
    }


def test_api_timeout_exhaustion_returns_provider_error(client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider(*[ProviderTimeoutError("Counting request timed out")] * 3))

    response = client.post(URL, json=PAYLOAD)

    assert provider.calls == 3
    assert response.status_code == 502
    assert response.json()["error"] == {"type": "provider_error", "message": "Counting request timed out"}


def test_api_non_retryable_error_single_attempt(client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider(ProviderError("Counting request failed with status 400")))

    response = client.post(URL, json=PAYLOAD)

    assert provider.calls == 1
    assert response.status_code == 502


def test_api_unexpected_error_single_attempt_and_hidden(client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider(RuntimeError("internal detail /srv/app.py")))
    lenient = TestClient(app, raise_server_exceptions=False, headers=client.headers)

    response = lenient.post(URL, json=PAYLOAD)

    assert provider.calls == 1
    assert response.status_code == 500
    assert "internal detail" not in response.text


def test_api_authentication_failure_makes_no_provider_call(anon_client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider())

    response = anon_client.post(URL, json=PAYLOAD, headers={"Authorization": "Bearer gw_live_invalid_test_key"})

    assert response.status_code == 401
    assert provider.calls == 0


def test_api_validation_failure_makes_no_provider_call(client: TestClient, use_provider: Any) -> None:
    provider = use_provider(CountingProvider())

    response = client.post(URL, json={"model": "mock", "messages": []})

    assert response.status_code == 422
    assert provider.calls == 0


def test_api_missing_provider_config_is_not_retried(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("gateway.services.retry.time.sleep", sleeps.append)

    response = client.post(URL, json={**PAYLOAD, "model": "groq"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "provider_not_configured"
    assert sleeps == []


def test_mock_provider_path_still_works(client: TestClient) -> None:
    response = client.post(URL, json=PAYLOAD)
    assert response.status_code == 200
    assert response.json()["content"] == "Mock response: Hi"
