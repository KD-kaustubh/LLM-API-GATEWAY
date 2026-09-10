"""Prometheus metrics. Every label takes values from a small, fixed set (never user input)."""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.gc_collector import GCCollector
from prometheus_client.process_collector import ProcessCollector

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
UNMATCHED_ROUTE = "unmatched"

HTTP_REQUESTS = Counter(
    "gateway_http_requests",
    "HTTP requests handled, by route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "gateway_http_request_duration_seconds",
    "HTTP request latency, by route template.",
    ["method", "route"],
    registry=REGISTRY,
)
AUTH_FAILURES = Counter(
    "gateway_auth_failures",
    "Rejected credentials, by reason.",
    ["reason"],
    registry=REGISTRY,
)
RATE_LIMIT_REJECTIONS = Counter(
    "gateway_rate_limit_rejections",
    "Requests rejected by the per-client rate limiter.",
    registry=REGISTRY,
)
INFERENCE_REQUESTS = Counter(
    "gateway_inference_requests",
    "Chat completion requests reaching the inference service, by outcome.",
    ["outcome"],
    registry=REGISTRY,
)
CACHE_LOOKUPS = Counter(
    "gateway_cache_lookups",
    "Response cache decisions: hit, miss, or bypass (disabled or ineligible).",
    ["result"],
    registry=REGISTRY,
)
COMPLETIONS = Counter(
    "gateway_completions",
    "Successful completions, by source provider ('cache' for cache hits).",
    ["provider"],
    registry=REGISTRY,
)
TOKENS = Counter(
    "gateway_tokens",
    "Provider-reported tokens for successful provider calls.",
    ["provider", "direction"],
    registry=REGISTRY,
)
PROVIDER_CALLS = Counter(
    "gateway_provider_calls",
    "Provider call attempts (each retry counts), by outcome.",
    ["provider", "outcome"],
    registry=REGISTRY,
)
PROVIDER_ERRORS = Counter(
    "gateway_provider_errors",
    "Failed provider call attempts, by error type.",
    ["provider", "error_type"],
    registry=REGISTRY,
)
PROVIDER_LATENCY = Histogram(
    "gateway_provider_call_duration_seconds",
    "Latency of individual provider call attempts.",
    ["provider"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
    registry=REGISTRY,
)
PROVIDER_RETRIES = Counter(
    "gateway_provider_retries",
    "Retries scheduled after transient provider failures.",
    ["provider"],
    registry=REGISTRY,
)


def method_label(method: str) -> str:
    return method if method in KNOWN_METHODS else "OTHER"


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
