import logging
from typing import Any

import pytest

from gateway.api.schemas import ChatCompletionRequest
from gateway.errors import ProviderError, TransientProviderError
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage
from gateway.services.cache import InMemoryCacheStore, ResponseCache
from gateway.services.inference import InferenceService
from gateway.services.retry import Retrier, RetryPolicy
from gateway.services.usage import InMemoryUsageRecorder, UsageRecord
from tests.conftest import FakeClock


class ScriptedProvider:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *errors: Exception, usage: TokenUsage = TokenUsage(11, 7, 18)) -> None:
        self._errors = list(errors)
        self._usage = usage
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return ProviderResponse(self.name, self.model, f"answer #{self.calls}", self._usage)


class FailingRecorder:
    def record(self, record: UsageRecord) -> None:
        raise OSError("database is locked at /secret/path/gateway.db")


def _request(**overrides: Any) -> ChatCompletionRequest:
    payload: dict[str, Any] = {
        "model": "scripted",
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0,
    }
    payload.update(overrides)
    return ChatCompletionRequest.model_validate(payload)


def _service(
    provider: ScriptedProvider,
    *,
    cache_store: InMemoryCacheStore | None = None,
    usage: Any = None,
    clock: FakeClock | None = None,
    max_retries: int = 2,
) -> InferenceService:
    cache = ResponseCache(cache_store, ttl_seconds=60, clock=clock or FakeClock()) if cache_store is not None else None
    latency_clock = FakeClock(0.0)
    return InferenceService(
        lambda _: provider,
        Retrier(RetryPolicy(max_retries=max_retries), sleep=lambda _: None, jitter=lambda: 0.0),
        cache=cache,
        usage=usage,
        clock=latency_clock,
    )


# --- usage -------------------------------------------------------------------


def test_success_records_usage_from_provider() -> None:
    usage = InMemoryUsageRecorder()
    result = _service(ScriptedProvider(), usage=usage).create_chat_completion(
        _request(), client_id="client-a", key_id="0123456789abcdef"
    )

    [record] = usage.records
    assert record.request_id == result.response.id
    assert (record.client_id, record.key_id) == ("client-a", "0123456789abcdef")
    assert (record.model, record.provider) == ("scripted-v1", "scripted")
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (11, 7, 18)
    assert record.cache_hit is False
    assert record.latency_ms >= 0
    assert record.created_at.tzinfo is not None


def test_missing_provider_usage_is_recorded_as_null() -> None:
    usage = InMemoryUsageRecorder()
    _service(ScriptedProvider(usage=TokenUsage()), usage=usage).create_chat_completion(_request())
    [record] = usage.records
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (None, None, None)


def test_provider_failure_records_no_usage() -> None:
    usage = InMemoryUsageRecorder()
    with pytest.raises(ProviderError):
        _service(ScriptedProvider(ProviderError("down")), usage=usage).create_chat_completion(_request())
    assert usage.records == []


def test_usage_failure_does_not_fail_successful_completion(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    result = _service(ScriptedProvider(), usage=FailingRecorder()).create_chat_completion(_request())

    assert result.response.content == "answer #1"
    assert "Usage recording failed" in caplog.text
    assert "/secret/path" not in caplog.text


def test_usage_record_contains_no_request_content_or_secrets() -> None:
    usage = InMemoryUsageRecorder()
    secret = "gw_live_invalid_test_key"
    _service(ScriptedProvider(), usage=usage).create_chat_completion(
        _request(messages=[{"role": "user", "content": secret}]), client_id="client-a"
    )
    assert secret not in repr(usage.records)


# --- cache -------------------------------------------------------------------


def test_cache_disabled_means_bypass_and_no_caching() -> None:
    provider = ScriptedProvider()
    service = _service(provider)

    assert service.create_chat_completion(_request()).cache_status == "BYPASS"
    assert service.create_chat_completion(_request()).cache_status == "BYPASS"
    assert provider.calls == 2


def test_ineligible_request_bypasses_cache() -> None:
    provider, store = ScriptedProvider(), InMemoryCacheStore(10)
    service = _service(provider, cache_store=store)

    for temperature in (None, 0.7):
        assert service.create_chat_completion(_request(temperature=temperature)).cache_status == "BYPASS"
    assert len(store) == 0
    assert provider.calls == 2


def test_miss_then_hit_without_provider_call() -> None:
    provider, store, usage = ScriptedProvider(), InMemoryCacheStore(10), InMemoryUsageRecorder()
    service = _service(provider, cache_store=store, usage=usage)

    miss = service.create_chat_completion(_request(), client_id="client-a", key_id="k1")
    hit = service.create_chat_completion(_request(), client_id="client-b", key_id="k2")

    assert (miss.cache_status, hit.cache_status) == ("MISS", "HIT")
    assert provider.calls == 1
    assert hit.response.content == miss.response.content == "answer #1"
    assert hit.response.provider == "scripted"
    assert hit.response.model == "scripted-v1"
    assert hit.response.id != miss.response.id
    assert hit.response.usage.model_dump() == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    miss_record, hit_record = usage.records
    assert (miss_record.cache_hit, miss_record.provider, miss_record.total_tokens) == (False, "scripted", 18)
    assert (hit_record.cache_hit, hit_record.provider) == (True, "cache")
    assert (hit_record.input_tokens, hit_record.output_tokens, hit_record.total_tokens) == (0, 0, 0)
    assert (hit_record.client_id, hit_record.key_id, hit_record.request_id) == ("client-b", "k2", hit.response.id)


def test_different_parameters_miss() -> None:
    provider, store = ScriptedProvider(), InMemoryCacheStore(10)
    service = _service(provider, cache_store=store)

    service.create_chat_completion(_request())
    assert service.create_chat_completion(_request(max_tokens=5)).cache_status == "MISS"
    assert service.create_chat_completion(_request(messages=[{"role": "user", "content": "Other"}])).cache_status == "MISS"
    assert provider.calls == 3


def test_expired_entry_calls_provider_again() -> None:
    provider, store, clock = ScriptedProvider(), InMemoryCacheStore(10), FakeClock()
    service = _service(provider, cache_store=store, clock=clock)

    service.create_chat_completion(_request())
    clock.advance(61)

    assert service.create_chat_completion(_request()).cache_status == "MISS"
    assert provider.calls == 2


def test_provider_failure_is_not_cached() -> None:
    provider, store = ScriptedProvider(ProviderError("bad")), InMemoryCacheStore(10)
    service = _service(provider, cache_store=store)

    with pytest.raises(ProviderError):
        service.create_chat_completion(_request())
    assert len(store) == 0

    assert service.create_chat_completion(_request()).cache_status == "MISS"
    assert provider.calls == 2


def test_exhausted_transient_failures_are_not_cached() -> None:
    provider = ScriptedProvider(*[TransientProviderError("blip")] * 3)
    store = InMemoryCacheStore(10)
    with pytest.raises(TransientProviderError):
        _service(provider, cache_store=store).create_chat_completion(_request())
    assert len(store) == 0


def test_retried_success_writes_cache_once() -> None:
    provider = ScriptedProvider(TransientProviderError("a"), TransientProviderError("b"))
    store = InMemoryCacheStore(10)
    writes: list[str] = []
    original_put = store.put
    store.put = lambda key, *a, **k: (writes.append(key), original_put(key, *a, **k))  # type: ignore[method-assign]

    _service(provider, cache_store=store).create_chat_completion(_request())

    assert provider.calls == 3
    assert len(writes) == 1


def test_cache_failures_fail_open() -> None:
    class BrokenStore(InMemoryCacheStore):
        def get(self, key: str, now: float) -> str | None:
            raise OSError("read failed")

        def put(self, *args: object, **kwargs: object) -> None:
            raise OSError("write failed")

    provider = ScriptedProvider()
    result = _service(provider, cache_store=BrokenStore(10)).create_chat_completion(_request())

    assert result.response.content == "answer #1"
    assert result.cache_status == "MISS"
    assert provider.calls == 1
