import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int  # 0 when allowed


class RateLimiter(Protocol):
    def allow(self, client_id: str) -> RateLimitResult: ...


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    """Per-client token bucket: bursts up to `limit`, refilling at `limit` per `window_seconds`.

    Process-local, so each worker process enforces its own limit. Buckets idle for a full window
    are equivalent to a new bucket and are pruned, so memory tracks recently active clients only.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit must be >= 1 and window_seconds must be > 0")
        self._limit = limit
        self._window = window_seconds
        self._refill_per_second = limit / window_seconds
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_prune = clock()

    def allow(self, client_id: str) -> RateLimitResult:
        with self._lock:
            now = self._clock()
            self._prune_idle(now)
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = self._buckets[client_id] = _Bucket(tokens=self._limit, updated_at=now)
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(self._limit, bucket.tokens + elapsed * self._refill_per_second)
                bucket.updated_at = now

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return RateLimitResult(True, self._limit, math.floor(bucket.tokens), 0)

            retry_after = math.ceil((1 - bucket.tokens) / self._refill_per_second)

        logger.info("Rate limit exceeded for client %s (retry after %ss)", client_id, retry_after)
        return RateLimitResult(False, self._limit, 0, max(1, retry_after))

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _prune_idle(self, now: float) -> None:
        if now - self._last_prune < self._window:
            return
        idle = [cid for cid, b in self._buckets.items() if now - b.updated_at >= self._window]
        for client_id in idle:
            del self._buckets[client_id]
        self._last_prune = now
