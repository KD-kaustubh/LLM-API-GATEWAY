from typing import Any

import httpx
from google.genai import Client, errors, types

from gateway.errors import ProviderError
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage

_ROLE_MAP = {"user": "user", "assistant": "model"}


def build_contents(request: ProviderRequest) -> list[types.Content]:
    return [
        types.Content(role=_ROLE_MAP[m.role], parts=[types.Part(text=m.content)])
        for m in request.messages
        if m.role != "system"
    ]


def build_config(request: ProviderRequest) -> types.GenerateContentConfig:
    system_parts = [m.content for m in request.messages if m.role == "system"]
    return types.GenerateContentConfig(
        system_instruction="\n\n".join(system_parts) if system_parts else None,
        temperature=request.temperature,
        max_output_tokens=request.max_tokens,
    )


def parse_response(response: Any, fallback_model: str) -> ProviderResponse:
    text = response.text
    if not text:
        raise ProviderError("Gemini returned no text content")

    usage = response.usage_metadata
    return ProviderResponse(
        provider=GeminiProvider.name,
        model=response.model_version or fallback_model,
        content=text,
        usage=TokenUsage(
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
            total_tokens=usage.total_token_count if usage else None,
        ),
    )


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        try:
            with Client(api_key=self._api_key) as client:
                response = client.models.generate_content(
                    model=self._model,
                    contents=build_contents(request),
                    config=build_config(request),
                )
        except errors.APIError as exc:
            raise ProviderError(f"Gemini request failed with status {exc.code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        return parse_response(response, self._model)
