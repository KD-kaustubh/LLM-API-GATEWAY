from typing import Any

from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, GroqError

from gateway.errors import ProviderError, ProviderTimeoutError, TransientProviderError
from gateway.providers.base import (
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    error_for_status,
)


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

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        try:
            # max_retries=0: the gateway's Retrier is the only retry layer, keeping attempts bounded.
            with Groq(
                api_key=self._api_key, timeout=self._timeout_seconds, max_retries=0
            ) as client:
                completion = client.chat.completions.create(
                    **build_request_kwargs(self._model, request)
                )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("Groq request timed out") from exc
        except APIConnectionError as exc:
            raise TransientProviderError("Groq connection failed") from exc
        except APIStatusError as exc:
            raise error_for_status("Groq", exc.status_code) from exc
        except GroqError as exc:
            raise ProviderError("Groq request failed") from exc
        return parse_completion(completion, self._model)
