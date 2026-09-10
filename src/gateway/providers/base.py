from dataclasses import dataclass
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    messages: tuple[Message, ...]
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class TokenUsage:
    """Token counts; None means the provider did not report that value."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    content: str
    usage: TokenUsage


class LLMProvider(Protocol):
    name: str

    def generate(self, request: ProviderRequest) -> ProviderResponse: ...
