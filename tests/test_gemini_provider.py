from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from gateway.errors import ProviderError
from gateway.providers import gemini as gemini_provider
from gateway.providers.base import Message, ProviderRequest
from gateway.providers.gemini import GeminiProvider, build_config, build_contents, parse_response

FAKE_KEY = "test-google-key-not-real"


def _response(text: str | None = "Hello from Gemini", usage: bool = True) -> types.GenerateContentResponse:
    data: dict[str, Any] = {"model_version": "gemini-2.5-flash"}
    data["candidates"] = (
        [{"content": {"role": "model", "parts": [{"text": text}]}}] if text is not None else []
    )
    if usage:
        data["usage_metadata"] = {
            "prompt_token_count": 9,
            "candidates_token_count": 3,
            "total_token_count": 12,
        }
    return types.GenerateContentResponse.model_validate(data)


class FakeClient:
    """Stands in for google.genai.Client; records generate_content() arguments."""

    calls: list[dict[str, Any]] = []
    result: Any = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def _generate_content(self, **kwargs: Any) -> Any:
        FakeClient.calls.append({"api_key": self.api_key, **kwargs})
        if isinstance(FakeClient.result, Exception):
            raise FakeClient.result
        return FakeClient.result


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.calls = []
    FakeClient.result = _response()
    monkeypatch.setattr(gemini_provider, "Client", FakeClient)
    return FakeClient


def _request(**kwargs: Any) -> ProviderRequest:
    return ProviderRequest(
        messages=(
            Message(role="system", content="Be brief"),
            Message(role="system", content="Use English"),
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello!"),
            Message(role="user", content="How are you?"),
        ),
        **kwargs,
    )


def test_contents_translation_maps_roles_and_drops_system() -> None:
    contents = build_contents(_request())

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [c.parts[0].text for c in contents] == ["Hi", "Hello!", "How are you?"]


def test_config_translation() -> None:
    config = build_config(_request(temperature=0.2, max_tokens=64))

    assert config.system_instruction == "Be brief\n\nUse English"
    assert config.temperature == 0.2
    assert config.max_output_tokens == 64


def test_config_without_system_or_options() -> None:
    config = build_config(ProviderRequest(messages=(Message(role="user", content="Hi"),)))

    assert config.system_instruction is None
    assert config.temperature is None
    assert config.max_output_tokens is None


def test_response_normalization() -> None:
    result = parse_response(_response(), fallback_model="configured")

    assert result.provider == "gemini"
    assert result.model == "gemini-2.5-flash"
    assert result.content == "Hello from Gemini"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (9, 3, 12)


def test_missing_usage_is_reported_as_none() -> None:
    result = parse_response(_response(usage=False), fallback_model="configured")
    assert result.usage.output_tokens is None


def test_empty_response_raises_provider_error() -> None:
    with pytest.raises(ProviderError):
        parse_response(_response(text=None), fallback_model="configured")


def test_generate_calls_sdk_with_config(fake_client: type[FakeClient]) -> None:
    result = GeminiProvider(api_key=FAKE_KEY, model="gemini-2.5-flash").generate(_request())

    assert result.content == "Hello from Gemini"
    [call] = fake_client.calls
    assert call["api_key"] == FAKE_KEY
    assert call["model"] == "gemini-2.5-flash"
    assert len(call["contents"]) == 3
    assert call["config"].system_instruction == "Be brief\n\nUse English"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (errors.ClientError(429, {"error": {"code": 429, "message": "quota"}}), "Gemini request failed with status 429"),
        (errors.ServerError(500, {"error": {"code": 500, "message": "boom"}}), "Gemini request failed with status 500"),
        (httpx.ConnectError("connection refused"), "Gemini request failed"),
    ],
)
def test_sdk_errors_become_provider_errors(
    fake_client: type[FakeClient], error: Exception, expected: str
) -> None:
    fake_client.result = error

    with pytest.raises(ProviderError) as exc_info:
        GeminiProvider(api_key=FAKE_KEY, model="m").generate(_request())

    assert exc_info.value.message == expected
    assert FAKE_KEY not in exc_info.value.message
