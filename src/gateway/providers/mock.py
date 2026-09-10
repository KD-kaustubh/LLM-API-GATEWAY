from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage


def _count_words(text: str) -> int:
    return len(text.split())


class MockProvider:
    """Deterministic offline provider; token counts are whitespace word counts, not real tokens."""

    name = "mock"
    model = "mock"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"), ""
        )
        content = f"Mock response: {last_user}"
        input_tokens = sum(_count_words(m.content) for m in request.messages)
        output_tokens = _count_words(content)
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=content,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
