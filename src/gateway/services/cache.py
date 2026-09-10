import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from gateway.api.schemas import ChatCompletionRequest
from gateway.providers.base import ProviderResponse

logger = logging.getLogger(__name__)

CACHE_KEY_VERSION = 1
MAX_CACHE_ENTRY_BYTES = 64 * 1024


@dataclass(frozen=True)
class CachedCompletion:
    model: str
    provider: str
    content: str


class CacheStore(Protocol):
    """Stores opaque payloads by key; implementations enforce expiry and a maximum entry count."""

    def get(self, key: str, now: float) -> str | None: ...

    def put(self, key: str, payload: str, model: str, now: float, expires_at: float) -> None: ...

    def purge_expired(self, now: float) -> int: ...


def is_cacheable(request: ChatCompletionRequest) -> bool:
    # Only explicitly deterministic requests: omitted temperature means the provider default (>0).
    return request.temperature is not None and request.temperature == 0


def build_cache_key(request: ChatCompletionRequest, provider: str, upstream_model: str) -> str:
    """SHA-256 of the canonical request. Contains no credentials, identity, or request ID."""
    canonical = {
        "v": CACHE_KEY_VERSION,
        "provider": provider,
        "upstream_model": upstream_model,
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "temperature": None if request.temperature is None else float(request.temperature),
        "max_tokens": request.max_tokens,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResponseCache:
    """Fail-open wrapper around a CacheStore: store errors are logged and treated as misses."""

    def __init__(
        self,
        store: CacheStore,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
        max_entry_bytes: int = MAX_CACHE_ENTRY_BYTES,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_entry_bytes = max_entry_bytes

    def get(self, key: str) -> CachedCompletion | None:
        try:
            payload = self._store.get(key, self._clock())
            if payload is None:
                return None
            data = json.loads(payload)
            return CachedCompletion(
                model=str(data["model"]), provider=str(data["provider"]), content=str(data["content"])
            )
        except Exception as exc:  # a broken cache must never fail the request
            logger.warning(
                "Cache read failed; continuing without cache: %s", type(exc).__name__,
                extra={"error_type": type(exc).__name__},
            )
            return None

    def put(self, key: str, result: ProviderResponse) -> None:
        payload = json.dumps(
            {
                "model": result.model,
                "provider": result.provider,
                "content": result.content,
                "usage": {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "total_tokens": result.usage.total_tokens,
                },
            },
            ensure_ascii=False,
        )
        if len(payload.encode("utf-8")) > self._max_entry_bytes:
            logger.info("Response too large to cache (%d byte limit)", self._max_entry_bytes)
            return
        try:
            now = self._clock()
            self._store.put(key, payload, result.model, now, now + self._ttl)
        except Exception as exc:  # a broken cache must never fail the request
            logger.warning(
                "Cache write failed; response returned uncached: %s", type(exc).__name__,
                extra={"error_type": type(exc).__name__},
            )


@dataclass
class _Entry:
    payload: str
    expires_at: float


class InMemoryCacheStore:
    """Process-local store with the same expiry and max-entry semantics as the SQLite store."""

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str, now: float) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.payload if entry and entry.expires_at > now else None

    def put(self, key: str, payload: str, model: str, now: float, expires_at: float) -> None:
        with self._lock:
            self._entries[key] = _Entry(payload, expires_at)
            self._purge_locked(now)
            overflow = len(self._entries) - self._max_entries
            if overflow > 0:
                for old_key in sorted(self._entries, key=lambda k: self._entries[k].expires_at)[:overflow]:
                    del self._entries[old_key]

    def purge_expired(self, now: float) -> int:
        with self._lock:
            return self._purge_locked(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _purge_locked(self, now: float) -> int:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for key in expired:
            del self._entries[key]
        return len(expired)
