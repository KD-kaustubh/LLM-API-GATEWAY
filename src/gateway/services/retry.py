import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from gateway.errors import TransientProviderError
from gateway.observability import metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter.

    Only TransientProviderError is retried. A retried chat completion may be billed twice, so
    everything else (bad requests, auth, unknown models, missing config, deterministic provider
    errors, unexpected exceptions) fails on the first attempt.
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 4.0

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def backoff(self, retry_number: int, jitter: float) -> float:
        """Delay before retry `retry_number` (1-based); `jitter` in [0, 1)."""
        ceiling = min(self.max_delay, self.base_delay * 2 ** (retry_number - 1))
        # "Equal jitter": at least half the ceiling so delays grow, randomized to avoid lockstep.
        return ceiling / 2 + ceiling / 2 * jitter


class Retrier:
    def __init__(
        self,
        policy: RetryPolicy,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._policy = policy
        self._sleep = sleep
        self._jitter = jitter

    def call(self, operation: Callable[[], T], provider: str) -> T:
        attempt = 1
        while True:
            try:
                return operation()
            except TransientProviderError as exc:
                error_type = type(exc).__name__
                if attempt >= self._policy.max_attempts:
                    logger.warning(
                        "Provider %s failed after %d attempt(s): %s",
                        provider, attempt, error_type,
                        extra={"provider": provider, "attempt": attempt, "error_type": error_type},
                    )
                    raise
                delay = self._policy.backoff(attempt, self._jitter())
                logger.warning(
                    "Transient failure from provider %s (attempt %d/%d, %s); retrying in %.2fs",
                    provider, attempt, self._policy.max_attempts, error_type, delay,
                    extra={
                        "provider": provider,
                        "attempt": attempt,
                        "max_attempts": self._policy.max_attempts,
                        "error_type": error_type,
                        "retry_in_s": round(delay, 3),
                    },
                )
                metrics.PROVIDER_RETRIES.labels(provider).inc()
                self._sleep(delay)
                attempt += 1
