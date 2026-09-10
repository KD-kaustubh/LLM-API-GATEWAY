from collections.abc import Callable
from uuid import uuid4

from gateway.api.schemas import ChatCompletionRequest, ChatCompletionResponse, Usage
from gateway.providers.base import LLMProvider, Message, ProviderRequest, ProviderResponse
from gateway.services.retry import Retrier, RetryPolicy

ProviderResolver = Callable[[str], LLMProvider]


def to_provider_request(request: ChatCompletionRequest) -> ProviderRequest:
    return ProviderRequest(
        messages=tuple(Message(role=m.role, content=m.content) for m in request.messages),
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


def to_api_response(result: ProviderResponse) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"req_{uuid4().hex}",
        model=result.model,
        provider=result.provider,
        content=result.content,
        usage=Usage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
        ),
    )


class InferenceService:
    def __init__(self, resolve_provider: ProviderResolver, retrier: Retrier | None = None) -> None:
        self._resolve_provider = resolve_provider
        self._retrier = retrier or Retrier(RetryPolicy(max_retries=0))

    def create_chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        # Provider resolution (unknown model, missing credentials) happens once, outside retries.
        provider = self._resolve_provider(request.model)
        provider_request = to_provider_request(request)
        result = self._retrier.call(lambda: provider.generate(provider_request), provider.name)
        return to_api_response(result)
