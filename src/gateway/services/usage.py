import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

CACHE_PROVIDER = "cache"


@dataclass(frozen=True)
class UsageRecord:
    """One completed request. Token fields are None when the provider did not report them."""

    request_id: str
    client_id: str
    key_id: str | None
    model: str
    provider: str  # CACHE_PROVIDER when served from cache (no provider call was made)
    created_at: datetime
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    cache_hit: bool


class UsageRecorder(Protocol):
    def record(self, record: UsageRecord) -> None: ...


class InMemoryUsageRecorder:
    """Bounded, process-local recorder for development and tests."""

    def __init__(self, max_records: int = 10_000) -> None:
        self._records: deque[UsageRecord] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records)
