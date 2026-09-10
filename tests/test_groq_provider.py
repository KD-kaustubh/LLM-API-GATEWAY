from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from groq.types.chat import ChatCompletion

from gateway.errors import ProviderError, ProviderTimeoutError, TransientProviderError
from gateway.providers import groq as groq_provider
from gateway.providers.base import Message, ProviderRequest
from gateway.providers.groq import GroqProvider, build_request_kwargs, parse_completion

FAKE_KEY = "test-groq-key-not-real"


def _completion(content: str | None = "Hello from Groq", usage: bool = True) -> ChatCompletion:
    data: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-3.3-70b-versatile",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    return ChatCompletion.model_validate(data)


class FakeGroq:
    """Stands in for groq.Groq; records constructor and create() arguments."""

    calls: list[dict[str, Any]] = []
    result: Any = None

    client_kwargs: dict[str, Any] = {}

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        self.api_key = api_key
        FakeGroq.client_kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def __enter__(self) -> "FakeGroq":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def _create(self, **kwargs: Any) -> Any:
        FakeGroq.calls.append({"api_key": self.api_key, **kwargs})
        if isinstance(FakeGroq.result, Exception):
            raise FakeGroq.result
        return FakeGroq.result


@pytest.fixture
def fake_groq(monkeypatch: pytest.MonkeyPatch) -> type[FakeGroq]:
    FakeGroq.calls = []
    FakeGroq.result = _completion()
    monkeypatch.setattr(groq_provider, "Groq", FakeGroq)
    return FakeGroq


def _request(**kwargs: Any) -> ProviderRequest:
    return ProviderRequest(
        messages=(Message(role="system", content="Be brief"), Message(role="user", content="Hi")),
        **kwargs,
    )


def test_request_translation_minimal() -> None:
    assert build_request_kwargs("model-x", _request()) == {
        "model": "model-x",
        "messages": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Hi"},
        ],
    }


def test_request_translation_with_options() -> None:
    kwargs = build_request_kwargs("model-x", _request(temperature=0.3, max_tokens=50))
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_completion_tokens"] == 50


def test_response_normalization() -> None:
    result = parse_completion(_completion(), fallback_model="configured")

    assert result.provider == "groq"
    assert result.model == "llama-3.3-70b-versatile"
    assert result.content == "Hello from Groq"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (12, 4, 16)


def test_missing_usage_is_reported_as_none() -> None:
    result = parse_completion(_completion(usage=False), fallback_model="configured")
    assert result.usage.input_tokens is None
    assert result.usage.total_tokens is None


def test_empty_content_raises_provider_error() -> None:
    with pytest.raises(ProviderError):
        parse_completion(_completion(content=None), fallback_model="configured")


def _provider(model: str = "m") -> GroqProvider:
    return GroqProvider(api_key=FAKE_KEY, model=model, timeout_seconds=12.5)


def test_generate_calls_sdk_with_config(fake_groq: type[FakeGroq]) -> None:
    result = _provider("llama-3.3-70b-versatile").generate(_request(max_tokens=20))

    assert result.content == "Hello from Groq"
    [call] = fake_groq.calls
    assert call["api_key"] == FAKE_KEY
    assert call["model"] == "llama-3.3-70b-versatile"
    assert call["max_completion_tokens"] == 20


def test_client_uses_timeout_and_disables_sdk_retries(fake_groq: type[FakeGroq]) -> None:
    _provider().generate(_request())
    assert fake_groq.client_kwargs == {"timeout": 12.5, "max_retries": 0}


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _status_error(status: int) -> groq.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", _GROQ_URL))
    return groq.APIStatusError(f"status {status} body with {FAKE_KEY}", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_message"),
    [
        pytest.param(
            groq.APITimeoutError(request=httpx.Request("POST", _GROQ_URL)),
            ProviderTimeoutError, "Groq request timed out", id="timeout",
        ),
        pytest.param(
            groq.APIConnectionError(request=httpx.Request("POST", _GROQ_URL)),
            TransientProviderError, "Groq connection failed", id="connection",
        ),
        *[
            pytest.param(
                _status_error(status), TransientProviderError,
                f"Groq request failed with status {status}", id=f"status-{status}",
            )
            for status in (408, 429, 502, 503, 504)
        ],
        *[
            pytest.param(
                _status_error(status), ProviderError,
                f"Groq request failed with status {status}", id=f"status-{status}",
            )
            for status in (400, 401, 403, 404, 422, 500)
        ],
    ],
)
def test_sdk_errors_are_classified(
    fake_groq: type[FakeGroq], error: Exception, expected_type: type, expected_message: str
) -> None:
    fake_groq.result = error

    with pytest.raises(ProviderError) as exc_info:
        _provider().generate(_request())

    assert type(exc_info.value) is expected_type
    assert exc_info.value.message == expected_message
    assert FAKE_KEY not in exc_info.value.message
