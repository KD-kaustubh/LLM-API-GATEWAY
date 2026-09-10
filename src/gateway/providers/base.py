from dataclasses import dataclass
from typing import Literal, Protocol

from gateway.errors import ProviderError, TransientProviderError

Role = Literal["system", "user", "assistant"]

# Upstream statuses that signal a temporary condition. Other 5xx (e.g. 500) may be
# deterministic for a given request, so they are not retried.
TRANSIENT_STATUS_CODES = frozenset({408, 429, 502, 503, 504})


def error_for_status(provider_label: str, status_code: int | None) -> ProviderError:
    message = f"{provider_label} request failed with status {status_code}"
    if status_code in TRANSIENT_STATUS_CODES:
        return TransientProviderError(message)
    return ProviderError(message)


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
