import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from gateway.api.schemas import ChatCompletionRequest, ChatCompletionResponse, Usage
from gateway.errors import (
    GatewayError,
    ProviderError,
    ProviderTimeoutError,
    TransientProviderError,
)
from gateway.observability import metrics
from gateway.observability.context import new_request_id
from gateway.providers.base import LLMProvider, Message, ProviderRequest, ProviderResponse
from gateway.services.cache import ResponseCache, build_cache_key, is_cacheable
from gateway.services.retry import Retrier, RetryPolicy
from gateway.services.usage import CACHE_PROVIDER, UsageRecord, UsageRecorder

logger = logging.getLogger(__name__)

ProviderResolver = Callable[[str], LLMProvider]
CacheStatus = Literal["HIT", "MISS", "BYPASS"]


@dataclass(frozen=True)
class CompletionResult:
    response: ChatCompletionResponse
    cache_status: CacheStatus


def _provider_error_type(exc: Exception) -> str:
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, TransientProviderError):
        return "transient_error"
    if isinstance(exc, ProviderError):
        return "provider_error"
    return "internal_error"


def to_provider_request(request: ChatCompletionRequest) -> ProviderRequest:
    return ProviderRequest(
        messages=tuple(Message(role=m.role, content=m.content) for m in request.messages),
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


def to_api_response(result: ProviderResponse, request_id: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=request_id,
        model=result.model,
        provider=result.provider,
        content=result.content,
        usage=Usage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
        ),
    )


class InferenceService:
    def __init__(
        self,
        resolve_provider: ProviderResolver,
        retrier: Retrier | None = None,
        cache: ResponseCache | None = None,
        usage: UsageRecorder | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._resolve_provider = resolve_provider
        self._retrier = retrier or Retrier(RetryPolicy(max_retries=0))
        self._cache = cache
        self._usage = usage
        self._clock = clock

    def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        client_id: str = "anonymous",
        key_id: str | None = None,
        request_id: str | None = None,
    ) -> CompletionResult:
        """`request_id` (the HTTP request's ID) becomes the response and usage-record ID."""
        try:
            result = self._complete(request, client_id, key_id, request_id or new_request_id())
        except GatewayError as exc:
            metrics.INFERENCE_REQUESTS.labels(exc.error_type).inc()
            raise
        except Exception:
            metrics.INFERENCE_REQUESTS.labels("internal_error").inc()
            raise
        metrics.INFERENCE_REQUESTS.labels("success").inc()
        return result

    def _complete(
        self, request: ChatCompletionRequest, client_id: str, key_id: str | None, request_id: str
    ) -> CompletionResult:
        started = self._clock()
        # Provider resolution (unknown model, missing credentials) happens once, outside retries.
        provider = self._resolve_provider(request.model)

        cache_key = None
        if self._cache is not None and is_cacheable(request):
            cache_key = build_cache_key(request, provider.name, provider.model)
            cached = self._cache.get(cache_key)
            if cached is not None:
                metrics.CACHE_LOOKUPS.labels("hit").inc()
                metrics.COMPLETIONS.labels(CACHE_PROVIDER).inc()
                response = ChatCompletionResponse(
                    id=request_id,
                    model=cached.model,
                    provider=cached.provider,
                    content=cached.content,
                    # No provider call was made, so this request consumed no provider tokens.
                    usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
                )
                self._record_usage(response, CACHE_PROVIDER, client_id, key_id, started, cache_hit=True)
                return CompletionResult(response, "HIT")
        metrics.CACHE_LOOKUPS.labels("miss" if cache_key else "bypass").inc()

        provider_request = to_provider_request(request)
        result = self._retrier.call(
            lambda: self._call_provider(provider, provider_request), provider.name
        )
        response = to_api_response(result, request_id)
        metrics.COMPLETIONS.labels(result.provider).inc()
        for direction, count in (("input", result.usage.input_tokens), ("output", result.usage.output_tokens)):
            if count:
                metrics.TOKENS.labels(result.provider, direction).inc(count)

        if cache_key is not None and self._cache is not None:
            self._cache.put(cache_key, result)
        self._record_usage(response, result.provider, client_id, key_id, started, cache_hit=False)
        return CompletionResult(response, "MISS" if cache_key else "BYPASS")

    def _call_provider(self, provider: LLMProvider, request: ProviderRequest) -> ProviderResponse:
        """One provider attempt, timed and counted; the Retrier decides whether to repeat it."""
        started = self._clock()
        try:
            result = provider.generate(request)
        except Exception as exc:
            error_type = _provider_error_type(exc)
            metrics.PROVIDER_CALLS.labels(provider.name, error_type).inc()
            metrics.PROVIDER_ERRORS.labels(provider.name, error_type).inc()
            raise
        finally:
            metrics.PROVIDER_LATENCY.labels(provider.name).observe(max(0.0, self._clock() - started))
        metrics.PROVIDER_CALLS.labels(provider.name, "success").inc()
        return result

    def _record_usage(
        self,
        response: ChatCompletionResponse,
        provider: str,
        client_id: str,
        key_id: str | None,
        started: float,
        cache_hit: bool,
    ) -> None:
        if self._usage is None:
            return
        record = UsageRecord(
            request_id=response.id,
            client_id=client_id,
            key_id=key_id,
            model=response.model,
            provider=provider,
            created_at=datetime.now(timezone.utc),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens,
            latency_ms=round((self._clock() - started) * 1000),
            cache_hit=cache_hit,
        )
        try:
            self._usage.record(record)
        except Exception as exc:  # fail open: a successful completion is still returned
            logger.warning(
                "Usage recording failed for request %s: %s", response.id, type(exc).__name__,
                extra={"error_type": type(exc).__name__},
            )
