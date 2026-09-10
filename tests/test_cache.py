import json
import logging
from typing import Any

import pytest

from gateway.api.schemas import ChatCompletionRequest
from gateway.persistence.database import Database
from gateway.persistence.repositories import SQLiteCacheStore
from gateway.providers.base import ProviderResponse, TokenUsage
from gateway.services.cache import (
    InMemoryCacheStore,
    ResponseCache,
    build_cache_key,
    is_cacheable,
)
from tests.conftest import FakeClock


def _request(**overrides: Any) -> ChatCompletionRequest:
    payload: dict[str, Any] = {
        "model": "mock",
        "messages": [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hi"}],
        "temperature": 0.0,
    }
    payload.update(overrides)
    return ChatCompletionRequest.model_validate(payload)


def _result(content: str = "cached answer") -> ProviderResponse:
    return ProviderResponse("mock", "mock", content, TokenUsage(3, 4, 7))


# --- eligibility -------------------------------------------------------------


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [(0.0, True), (0, True), (None, False), (0.1, False), (1.0, False), (2.0, False)],
)
def test_only_zero_temperature_is_cacheable(temperature: float | None, expected: bool) -> None:
    assert is_cacheable(_request(temperature=temperature)) is expected


# --- cache key ---------------------------------------------------------------


def test_cache_key_is_stable_sha256() -> None:
    first = build_cache_key(_request(), "mock", "mock")
    second = build_cache_key(_request(), "mock", "mock")
    assert first == second
    assert len(first) == 64
    int(first, 16)


def test_int_and_float_temperature_share_a_key() -> None:
    assert build_cache_key(_request(temperature=0), "mock", "mock") == build_cache_key(
        _request(temperature=0.0), "mock", "mock"
    )


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(lambda: build_cache_key(_request(model="groq"), "mock", "mock"), id="model"),
        pytest.param(lambda: build_cache_key(_request(max_tokens=10), "mock", "mock"), id="max_tokens"),
        pytest.param(lambda: build_cache_key(_request(temperature=0.5), "mock", "mock"), id="temperature"),
        pytest.param(lambda: build_cache_key(_request(messages=[{"role": "user", "content": "Hi!"}]), "mock", "mock"), id="content"),
        pytest.param(lambda: build_cache_key(_request(messages=[{"role": "user", "content": "Be brief"}, {"role": "user", "content": "Hi"}]), "mock", "mock"), id="role"),
        pytest.param(lambda: build_cache_key(_request(messages=[{"role": "user", "content": "Hi"}, {"role": "system", "content": "Be brief"}]), "mock", "mock"), id="order"),
        pytest.param(lambda: build_cache_key(_request(), "groq", "mock"), id="provider"),
        pytest.param(lambda: build_cache_key(_request(), "mock", "other-upstream-model"), id="upstream-model"),
    ],
)
def test_every_semantic_field_changes_the_key(variant: Any) -> None:
    assert variant() != build_cache_key(_request(), "mock", "mock")


def test_cache_key_contains_no_request_content_or_credentials() -> None:
    secret_like = "gw_live_invalid_test_key"
    key = build_cache_key(_request(messages=[{"role": "user", "content": secret_like}]), "mock", "mock")
    assert secret_like not in key
    assert "mock" not in key


# --- store behaviour (both implementations) ------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, database: Database) -> Any:
    if request.param == "memory":
        return InMemoryCacheStore(max_entries=3)
    return SQLiteCacheStore(database, max_entries=3)


def test_store_round_trip(store: Any) -> None:
    store.put("k1", "payload", "m", now=100, expires_at=200)
    assert store.get("k1", now=150) == "payload"


def test_expired_entry_misses(store: Any) -> None:
    store.put("k1", "payload", "m", now=100, expires_at=200)
    assert store.get("k1", now=200) is None
    assert store.get("k1", now=500) is None


def test_purge_removes_only_expired(store: Any) -> None:
    store.put("old", "p", "m", now=100, expires_at=150)
    store.put("fresh", "p", "m", now=100, expires_at=400)
    assert store.purge_expired(now=200) == 1
    assert store.get("fresh", now=200) == "p"
    assert len(store) == 1


def test_writes_purge_expired_entries(store: Any) -> None:
    store.put("old", "p", "m", now=100, expires_at=150)
    store.put("new", "p", "m", now=200, expires_at=500)
    assert len(store) == 1


def test_max_entries_evicts_soonest_expiring(store: Any) -> None:
    for i in range(5):
        store.put(f"k{i}", f"p{i}", "m", now=100 + i, expires_at=1000 + i)

    assert len(store) == 3
    assert [store.get(f"k{i}", now=110) for i in range(5)] == [None, None, "p2", "p3", "p4"]


def test_rewriting_a_key_does_not_grow_the_store(store: Any) -> None:
    for i in range(10):
        store.put("same", f"p{i}", "m", now=100 + i, expires_at=1000 + i)
    assert len(store) == 1
    assert store.get("same", now=120) == "p9"


# --- ResponseCache ------------------------------------------------------------


def test_response_cache_round_trip_and_ttl(clock: FakeClock) -> None:
    cache = ResponseCache(InMemoryCacheStore(10), ttl_seconds=60, clock=clock)
    cache.put("k", _result())

    hit = cache.get("k")
    assert hit is not None
    assert (hit.model, hit.provider, hit.content) == ("mock", "mock", "cached answer")

    clock.advance(60)
    assert cache.get("k") is None


def test_payload_stores_no_secrets_and_is_plain_json(clock: FakeClock) -> None:
    store = InMemoryCacheStore(10)
    ResponseCache(store, ttl_seconds=60, clock=clock).put("k", _result())
    payload = json.loads(store.get("k", clock()))
    assert set(payload) == {"model", "provider", "content", "usage"}


def test_oversized_response_is_not_cached(clock: FakeClock) -> None:
    store = InMemoryCacheStore(10)
    cache = ResponseCache(store, ttl_seconds=60, clock=clock, max_entry_bytes=100)
    cache.put("k", _result("x" * 200))
    assert len(store) == 0


class BrokenStore:
    def get(self, key: str, now: float) -> str | None:
        raise OSError("disk I/O error at /secret/path/gateway.db")

    def put(self, *args: object) -> None:
        raise OSError("disk full at /secret/path/gateway.db")

    def purge_expired(self, now: float) -> int:
        raise OSError("nope")


def test_cache_read_failure_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    assert ResponseCache(BrokenStore(), ttl_seconds=60).get("k") is None
    assert "Cache read failed" in caplog.text
    assert "/secret/path" not in caplog.text


def test_cache_write_failure_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    ResponseCache(BrokenStore(), ttl_seconds=60).put("k", _result())
    assert "Cache write failed" in caplog.text
    assert "/secret/path" not in caplog.text


@pytest.mark.parametrize("payload", ["not json", "{}", '{"model": "m"}', "[1, 2]"])
def test_malformed_payload_is_a_miss(clock: FakeClock, payload: str) -> None:
    store = InMemoryCacheStore(10)
    store.put("k", payload, "m", now=clock(), expires_at=clock() + 60)
    assert ResponseCache(store, ttl_seconds=60, clock=clock).get("k") is None
