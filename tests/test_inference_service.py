import pytest

from gateway.api.schemas import ChatCompletionRequest
from gateway.errors import ProviderError, UnsupportedModelError
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage
from gateway.services.inference import InferenceService


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            provider=self.name,
            model="recording-model-v1",
            content="recorded",
            usage=TokenUsage(input_tokens=1, output_tokens=None, total_tokens=None),
        )


def _request(model: str = "recording") -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.5,
            "max_tokens": 10,
        }
    )


def test_resolves_provider_by_model_name() -> None:
    provider = RecordingProvider()
    resolved: list[str] = []

    def resolve(model: str) -> RecordingProvider:
        resolved.append(model)
        return provider

    InferenceService(resolve).create_chat_completion(_request("some-model"))

    assert resolved == ["some-model"]


def test_invokes_provider_with_translated_request() -> None:
    provider = RecordingProvider()

    InferenceService(lambda _: provider).create_chat_completion(_request())

    [sent] = provider.requests
    assert [(m.role, m.content) for m in sent.messages] == [("system", "sys"), ("user", "hello")]
    assert sent.temperature == 0.5
    assert sent.max_tokens == 10


def test_builds_normalized_response() -> None:
    response = InferenceService(lambda _: RecordingProvider()).create_chat_completion(_request())

    assert response.id.startswith("req_")
    assert response.object == "chat.completion"
    assert response.model == "recording-model-v1"
    assert response.provider == "recording"
    assert response.content == "recorded"
    assert response.usage.input_tokens == 1
    assert response.usage.output_tokens is None


def test_resolver_errors_propagate() -> None:
    def resolve(model: str) -> RecordingProvider:
        raise UnsupportedModelError("nope")

    with pytest.raises(UnsupportedModelError):
        InferenceService(resolve).create_chat_completion(_request())


def test_provider_errors_propagate() -> None:
    class Broken(RecordingProvider):
        def generate(self, request: ProviderRequest) -> ProviderResponse:
            raise ProviderError("down")

    with pytest.raises(ProviderError):
        InferenceService(lambda _: Broken()).create_chat_completion(_request())
