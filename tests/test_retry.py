import logging
from collections.abc import Callable

import pytest

from gateway.errors import (
    AuthenticationError,
    GatewayError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    TransientProviderError,
    UnsupportedModelError,
)
from gateway.services.retry import Retrier, RetryPolicy

POLICY = RetryPolicy(max_retries=2, base_delay=0.5, max_delay=4.0)


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class ScriptedOperation:
    """Raises the scripted errors in order, then returns 'ok'."""

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return "ok"


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


def _retrier(sleep: Callable[[float], None], policy: RetryPolicy = POLICY) -> Retrier:
    return Retrier(policy, sleep=sleep, jitter=lambda: 0.5)


def test_first_attempt_success_calls_once(sleep: FakeSleep) -> None:
    operation = ScriptedOperation()
    assert _retrier(sleep).call(operation, "fake") == "ok"
    assert operation.calls == 1
    assert sleep.delays == []


def test_one_transient_failure_then_success(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(TransientProviderError("blip"))
    assert _retrier(sleep).call(operation, "fake") == "ok"
    assert operation.calls == 2
    assert len(sleep.delays) == 1


def test_two_transient_failures_then_success(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(TransientProviderError("a"), ProviderTimeoutError("b"))
    assert _retrier(sleep).call(operation, "fake") == "ok"
    assert operation.calls == 3
    assert len(sleep.delays) == 2


def test_retries_are_bounded_and_last_error_is_raised(sleep: FakeSleep) -> None:
    last = TransientProviderError("Fake request failed with status 503")
    operation = ScriptedOperation(TransientProviderError("1"), TransientProviderError("2"), last, TransientProviderError("never"))

    with pytest.raises(TransientProviderError) as exc_info:
        _retrier(sleep).call(operation, "fake")

    assert exc_info.value is last
    assert operation.calls == POLICY.max_attempts == 3
    assert len(sleep.delays) == POLICY.max_retries


def test_zero_retries_means_single_attempt(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(TransientProviderError("blip"))
    with pytest.raises(TransientProviderError):
        _retrier(sleep, RetryPolicy(max_retries=0)).call(operation, "fake")
    assert operation.calls == 1


def test_timeout_is_retried(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(ProviderTimeoutError("Fake request timed out"))
    assert _retrier(sleep).call(operation, "fake") == "ok"
    assert operation.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderError("Fake request failed with status 400"), id="non-retryable-provider"),
        pytest.param(AuthenticationError(), id="authentication"),
        pytest.param(UnsupportedModelError("nope"), id="unsupported-model"),
        pytest.param(ProviderNotConfiguredError("missing key"), id="missing-config"),
        pytest.param(GatewayError("generic"), id="generic-gateway"),
        pytest.param(RuntimeError("bug"), id="unexpected"),
        pytest.param(ValueError("bad value"), id="value-error"),
    ],
)
def test_non_retryable_errors_fail_after_one_attempt(sleep: FakeSleep, error: Exception) -> None:
    operation = ScriptedOperation(error)

    with pytest.raises(type(error)):
        _retrier(sleep).call(operation, "fake")

    assert operation.calls == 1
    assert sleep.delays == []


def test_sleeps_follow_backoff_schedule(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(TransientProviderError("1"), TransientProviderError("2"))
    _retrier(sleep).call(operation, "fake")
    assert sleep.delays == [POLICY.backoff(1, 0.5), POLICY.backoff(2, 0.5)]


@pytest.mark.parametrize(
    ("retry_number", "jitter", "expected"),
    [
        (1, 0.0, 0.25),
        (1, 0.5, 0.375),
        (2, 0.0, 0.5),
        (3, 0.0, 1.0),
        (4, 0.0, 2.0),
        (5, 0.0, 2.0),   # ceiling capped at max_delay=4.0
        (50, 0.0, 2.0),
        (50, 0.999, 3.998),
    ],
)
def test_backoff_is_deterministic_for_given_jitter(retry_number: int, jitter: float, expected: float) -> None:
    assert POLICY.backoff(retry_number, jitter) == pytest.approx(expected)


def test_backoff_never_exceeds_max_delay() -> None:
    for retry_number in range(1, 64):
        for jitter in (0.0, 0.5, 0.9999):
            delay = POLICY.backoff(retry_number, jitter)
            assert 0 <= delay <= POLICY.max_delay


def test_backoff_grows_until_cap() -> None:
    delays = [POLICY.backoff(n, 0.0) for n in range(1, 6)]
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


def test_zero_base_delay_means_no_waiting(sleep: FakeSleep) -> None:
    operation = ScriptedOperation(TransientProviderError("1"))
    _retrier(sleep, RetryPolicy(max_retries=1, base_delay=0, max_delay=0)).call(operation, "fake")
    assert sleep.delays == [0]


def test_retry_logs_omit_error_details(sleep: FakeSleep, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_detail = "upstream said: key=gw_live_invalid_test_key"
    operation = ScriptedOperation(*[TransientProviderError(secret_detail)] * 3)

    with pytest.raises(TransientProviderError):
        _retrier(sleep).call(operation, "fake")

    assert "attempt 1/3" in caplog.text
    assert "TransientProviderError" in caplog.text
    assert secret_detail not in caplog.text
