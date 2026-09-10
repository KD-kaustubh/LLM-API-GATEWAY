from typing import Any

from groq import Groq, GroqError, APIStatusError

from gateway.errors import ProviderError
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage


def build_request_kwargs(model: str, request: ProviderRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
    }
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if request.max_tokens is not None:
        kwargs["max_completion_tokens"] = request.max_tokens
    return kwargs


def parse_completion(completion: Any, fallback_model: str) -> ProviderResponse:
    if not completion.choices or completion.choices[0].message.content is None:
        raise ProviderError("Groq returned no content")

    usage = completion.usage
    return ProviderResponse(
        provider=GroqProvider.name,
        model=completion.model or fallback_model,
        content=completion.choices[0].message.content,
        usage=TokenUsage(
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
        ),
    )


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        try:
            with Groq(api_key=self._api_key) as client:
                completion = client.chat.completions.create(
                    **build_request_kwargs(self._model, request)
                )
        except APIStatusError as exc:
            raise ProviderError(f"Groq request failed with status {exc.status_code}") from exc
        except GroqError as exc:
            raise ProviderError("Groq request failed") from exc
        return parse_completion(completion, self._model)
