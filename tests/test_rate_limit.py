import threading

import pytest

from gateway.rate_limit import InMemoryRateLimiter
from tests.conftest import FakeClock


def _limiter(clock: FakeClock, limit: int = 3, window: float = 60) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(limit=limit, window_seconds=window, clock=clock)


def test_first_request_allowed(clock: FakeClock) -> None:
    result = _limiter(clock).allow("a")
    assert result.allowed
    assert result.limit == 3
    assert result.remaining == 2
    assert result.retry_after_seconds == 0


def test_requests_up_to_limit_allowed_with_decreasing_remaining(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    results = [limiter.allow("a") for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, True]
    assert [r.remaining for r in results] == [2, 1, 0]


def test_request_over_limit_rejected(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")

    result = limiter.allow("a")

    assert not result.allowed
    assert result.remaining == 0
    assert result.retry_after_seconds == 20  # one token per 60/3 seconds


def test_retry_after_shrinks_as_time_passes(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")

    clock.advance(15)

    assert limiter.allow("a").retry_after_seconds == 5


def test_retry_after_is_at_least_one_second(clock: FakeClock) -> None:
    limiter = _limiter(clock, limit=1000, window=1)
    for _ in range(1000):
        limiter.allow("a")
    assert limiter.allow("a").retry_after_seconds == 1


def test_rejected_requests_do_not_consume_tokens(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")
    for _ in range(10):
        assert not limiter.allow("a").allowed

    clock.advance(20)

    assert limiter.allow("a").allowed


def test_clients_have_independent_limits(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")

    assert not limiter.allow("a").allowed
    assert limiter.allow("b").allowed
    assert limiter.allow("b").remaining == 1


def test_limit_fully_resets_after_window(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")

    clock.advance(60)

    results = [limiter.allow("a") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]


def test_tokens_refill_gradually(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")

    clock.advance(19.9)
    assert not limiter.allow("a").allowed
    clock.advance(0.1)
    assert limiter.allow("a").allowed


def test_bucket_never_exceeds_limit_after_long_idle(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    limiter.allow("a")
    clock.advance(10_000)
    results = [limiter.allow("a") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]


def test_clock_going_backwards_grants_no_tokens(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.allow("a")
    clock.advance(-100)
    assert not limiter.allow("a").allowed


def test_idle_buckets_are_pruned(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    for i in range(500):
        limiter.allow(f"client-{i}")
    assert limiter.tracked_clients() == 500

    clock.advance(60)
    limiter.allow("fresh")

    assert limiter.tracked_clients() == 1


def test_pruning_does_not_reset_active_clients(clock: FakeClock) -> None:
    limiter = _limiter(clock)
    clock.advance(59)
    for _ in range(3):
        limiter.allow("busy")

    clock.advance(1)  # prune runs now; "busy" was active 1s ago and keeps its empty bucket

    assert not limiter.allow("busy").allowed


def test_concurrent_requests_cannot_exceed_limit(clock: FakeClock) -> None:
    limiter = _limiter(clock, limit=25)
    threads = 100
    barrier = threading.Barrier(threads)
    allowed: list[bool] = []
    allowed_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        result = limiter.allow("shared")
        with allowed_lock:
            allowed.append(result.allowed)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert len(allowed) == threads
    assert allowed.count(True) == 25


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (10, 0), (10, -5)])
def test_invalid_configuration_rejected(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        InMemoryRateLimiter(limit=limit, window_seconds=window)
