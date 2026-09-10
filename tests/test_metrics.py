"""Prometheus metrics: endpoint access policy, counters, bounded labels, and no secrets."""

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway.api.routes import get_inference_service
from gateway.api.security import get_rate_limiter
from gateway.auth.models import IssuedApiKey
from gateway.config import Settings
from gateway.errors import ProviderError, ProviderTimeoutError, TransientProviderError
from gateway.main import app
from gateway.observability.metrics import REGISTRY
from gateway.providers.base import ProviderRequest, ProviderResponse, TokenUsage
from gateway.rate_limit import InMemoryRateLimiter
from gateway.services.inference import InferenceService
from gateway.services.retry import Retrier, RetryPolicy
from tests.conftest import FakeClock, secret_of

URL = "/v1/chat/completions"
PROMPT_CANARY = "prompt-canary-must-never-be-exported"


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _payload(**overrides: Any) -> dict[str, Any]:
    return {"model": "mock", "messages": [{"role": "user", "content": "Hi"}], **overrides}


class _Scripted:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        if self._errors:
            raise self._errors.pop(0)
        return ProviderResponse(self.name, self.model, "ok", TokenUsage(4, 6, 10))


def _use(provider: _Scripted, max_retries: int = 2) -> None:
    retrier = Retrier(RetryPolicy(max_retries=max_retries), sleep=lambda _: None, jitter=lambda: 0.0)
    app.dependency_overrides[get_inference_service] = lambda: InferenceService(lambda _: provider, retrier)


# --- endpoint access policy ------------------------------------------------------


def test_metrics_open_in_development_without_token(anon_client: TestClient) -> None:
    response = anon_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "gateway_http_requests_total" in response.text


def test_metrics_hidden_outside_development_without_token(anon_client: TestClient, settings: Settings) -> None:
    settings.app_env = "production"
    response = anon_client.get("/metrics")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


@pytest.mark.parametrize("app_env", ["development", "production"])
def test_metrics_token_required_when_configured(anon_client: TestClient, settings: Settings, app_env: str) -> None:
    token = secrets.token_urlsafe(32)
    settings.app_env = app_env
    settings.metrics_token = SecretStr(token)

    missing = anon_client.get("/metrics")
    wrong = anon_client.get("/metrics", headers={"Authorization": f"Bearer {token}x"})
    ok = anon_client.get("/metrics", headers={"Authorization": f"Bearer {token}"})

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == {"error": {"type": "authentication_error", "message": "Invalid metrics token"}}
    assert token not in wrong.text
    assert ok.status_code == 200


def test_gateway_api_key_does_not_grant_metrics(anon_client: TestClient, settings: Settings, auth_headers: dict[str, str]) -> None:
    settings.metrics_token = SecretStr(secrets.token_urlsafe(32))
    assert anon_client.get("/metrics", headers=auth_headers).status_code == 401


def test_metrics_token_failure_is_counted(anon_client: TestClient, settings: Settings) -> None:
    settings.metrics_token = SecretStr(secrets.token_urlsafe(32))
    before = sample("gateway_auth_failures_total", reason="metrics_token")
    anon_client.get("/metrics")
    assert sample("gateway_auth_failures_total", reason="metrics_token") == before + 1


# --- HTTP metrics ------------------------------------------------------------------


def test_http_requests_counted_by_route_and_status(client: TestClient, anon_client: TestClient) -> None:
    ok_before = sample("gateway_http_requests_total", method="POST", route=URL, status="200")
    unauth_before = sample("gateway_http_requests_total", method="POST", route=URL, status="401")
    health_before = sample("gateway_http_requests_total", method="GET", route="/health", status="200")

    client.post(URL, json=_payload())
    client.post(URL, json=_payload())
    anon_client.post(URL, json=_payload())
    anon_client.get("/health")

    assert sample("gateway_http_requests_total", method="POST", route=URL, status="200") == ok_before + 2
    assert sample("gateway_http_requests_total", method="POST", route=URL, status="401") == unauth_before + 1
    assert sample("gateway_http_requests_total", method="GET", route="/health", status="200") == health_before + 1


def test_http_latency_histogram_observed(anon_client: TestClient) -> None:
    before = sample("gateway_http_request_duration_seconds_count", method="GET", route="/health")
    anon_client.get("/health")
    assert sample("gateway_http_request_duration_seconds_count", method="GET", route="/health") == before + 1
    assert sample("gateway_http_request_duration_seconds_sum", method="GET", route="/health") > 0


def test_labels_stay_bounded_for_unknown_paths_and_methods(anon_client: TestClient) -> None:
    before = sample("gateway_http_requests_total", method="OTHER", route="unmatched", status="404")

    for i in range(5):
        anon_client.request("BREW", f"/random/{i}/{secrets.token_hex(8)}")

    assert sample("gateway_http_requests_total", method="OTHER", route="unmatched", status="404") == before + 5
    exported = anon_client.get("/metrics").text
    assert "/random/" not in exported
    assert "BREW" not in exported


# --- auth and rate limiting -----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing_credentials"),
        ("Bearer gw_live_invalid_test_key", "malformed_key"),
        ("Bearer gw_live_0123456789abcdef_" + "A" * 43, "unknown_key"),
    ],
)
def test_auth_failures_counted_by_reason(anon_client: TestClient, header: str | None, reason: str) -> None:
    before = sample("gateway_auth_failures_total", reason=reason)
    anon_client.post(URL, json=_payload(), headers={"Authorization": header} if header else {})
    assert sample("gateway_auth_failures_total", reason=reason) == before + 1


def test_rate_limit_rejections_counted(client: TestClient, clock: FakeClock) -> None:
    limiter = InMemoryRateLimiter(limit=1, window_seconds=60, clock=clock)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    before = sample("gateway_rate_limit_rejections_total")

    statuses = [client.post(URL, json=_payload()).status_code for _ in range(3)]

    assert statuses == [200, 429, 429]
    assert sample("gateway_rate_limit_rejections_total") == before + 2


# --- inference, provider, retries, cache, usage -------------------------------------------


def test_successful_inference_metrics(client: TestClient) -> None:
    _use(_Scripted())
    before = {
        "inference": sample("gateway_inference_requests_total", outcome="success"),
        "calls": sample("gateway_provider_calls_total", provider="scripted", outcome="success"),
        "completions": sample("gateway_completions_total", provider="scripted"),
        "in": sample("gateway_tokens_total", provider="scripted", direction="input"),
        "out": sample("gateway_tokens_total", provider="scripted", direction="output"),
        "latency": sample("gateway_provider_call_duration_seconds_count", provider="scripted"),
        "bypass": sample("gateway_cache_lookups_total", result="bypass"),
    }

    assert client.post(URL, json=_payload(model="scripted")).status_code == 200

    assert sample("gateway_inference_requests_total", outcome="success") == before["inference"] + 1
    assert sample("gateway_provider_calls_total", provider="scripted", outcome="success") == before["calls"] + 1
    assert sample("gateway_completions_total", provider="scripted") == before["completions"] + 1
    assert sample("gateway_tokens_total", provider="scripted", direction="input") == before["in"] + 4
    assert sample("gateway_tokens_total", provider="scripted", direction="output") == before["out"] + 6
    assert sample("gateway_provider_call_duration_seconds_count", provider="scripted") == before["latency"] + 1
    assert sample("gateway_cache_lookups_total", result="bypass") == before["bypass"] + 1


def test_retry_and_provider_error_metrics(client: TestClient) -> None:
    _use(_Scripted(TransientProviderError("a"), ProviderTimeoutError("b")))
    retries = sample("gateway_provider_retries_total", provider="scripted")
    transient = sample("gateway_provider_errors_total", provider="scripted", error_type="transient_error")
    timeouts = sample("gateway_provider_errors_total", provider="scripted", error_type="timeout")
    attempts = sample("gateway_provider_call_duration_seconds_count", provider="scripted")

    assert client.post(URL, json=_payload(model="scripted")).status_code == 200

    assert sample("gateway_provider_retries_total", provider="scripted") == retries + 2
    assert sample("gateway_provider_errors_total", provider="scripted", error_type="transient_error") == transient + 1
    assert sample("gateway_provider_errors_total", provider="scripted", error_type="timeout") == timeouts + 1
    assert sample("gateway_provider_call_duration_seconds_count", provider="scripted") == attempts + 3


def test_failed_inference_counted_by_outcome(client: TestClient) -> None:
    _use(_Scripted(ProviderError("bad request")))
    provider_errors = sample("gateway_provider_errors_total", provider="scripted", error_type="provider_error")
    failed = sample("gateway_inference_requests_total", outcome="provider_error")

    assert client.post(URL, json=_payload(model="scripted")).status_code == 502

    assert sample("gateway_provider_errors_total", provider="scripted", error_type="provider_error") == provider_errors + 1
    assert sample("gateway_inference_requests_total", outcome="provider_error") == failed + 1


def test_unsupported_and_unconfigured_models_counted(client: TestClient) -> None:
    unsupported = sample("gateway_inference_requests_total", outcome="unsupported_model")
    unconfigured = sample("gateway_inference_requests_total", outcome="provider_not_configured")

    client.post(URL, json=_payload(model="gpt-unknown"))
    client.post(URL, json=_payload(model="groq"))

    assert sample("gateway_inference_requests_total", outcome="unsupported_model") == unsupported + 1
    assert sample("gateway_inference_requests_total", outcome="provider_not_configured") == unconfigured + 1


def test_cache_hit_and_miss_metrics(client: TestClient, settings: Settings) -> None:
    settings.cache_enabled = True
    hits = sample("gateway_cache_lookups_total", result="hit")
    misses = sample("gateway_cache_lookups_total", result="miss")
    cached = sample("gateway_completions_total", provider="cache")

    first = client.post(URL, json=_payload(temperature=0))
    second = client.post(URL, json=_payload(temperature=0))

    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
    assert sample("gateway_cache_lookups_total", result="miss") == misses + 1
    assert sample("gateway_cache_lookups_total", result="hit") == hits + 1
    assert sample("gateway_completions_total", provider="cache") == cached + 1


# --- no secrets or content in the exposition ------------------------------------------------


def test_metrics_output_contains_no_secrets_or_content(
    client: TestClient, anon_client: TestClient, issued_key: IssuedApiKey
) -> None:
    client.post(URL, json=_payload(messages=[{"role": "user", "content": PROMPT_CANARY}]))
    anon_client.post(URL, json=_payload(), headers={"Authorization": "Bearer gw_live_invalid_test_key"})

    exported = anon_client.get("/metrics").text

    for forbidden in (
        issued_key.api_key, secret_of(issued_key.api_key), issued_key.record.key_hash,
        issued_key.record.key_id, "test-client", PROMPT_CANARY, "Mock response", "req_", "gw_live",
    ):
        assert forbidden not in exported
