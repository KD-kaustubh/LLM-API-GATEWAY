import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.api.security import get_rate_limiter
from gateway.auth.models import IssuedApiKey
from gateway.auth.service import ApiKeyService
from gateway.main import app
from gateway.rate_limit import InMemoryRateLimiter
from tests.conftest import FakeClock

URL = "/v1/chat/completions"
PAYLOAD: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hi"}]}
LIMIT = 3
WINDOW = 60


@pytest.fixture
def limiter(anon_client: TestClient, clock: FakeClock) -> InMemoryRateLimiter:
    limiter = InMemoryRateLimiter(limit=LIMIT, window_seconds=WINDOW, clock=clock)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    return limiter


def _bearer(key: IssuedApiKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {key.api_key}"}


def _exhaust(client: TestClient, headers: dict[str, str] | None = None) -> None:
    for _ in range(LIMIT):
        assert client.post(URL, json=PAYLOAD, headers=headers).status_code == 200


@pytest.mark.usefixtures("limiter")
def test_requests_within_limit_succeed_with_headers(client: TestClient) -> None:
    responses = [client.post(URL, json=PAYLOAD) for _ in range(LIMIT)]

    assert [r.status_code for r in responses] == [200] * LIMIT
    assert [r.headers["X-RateLimit-Limit"] for r in responses] == [str(LIMIT)] * LIMIT
    assert [r.headers["X-RateLimit-Remaining"] for r in responses] == ["2", "1", "0"]


@pytest.mark.usefixtures("limiter")
def test_exceeding_limit_returns_429(client: TestClient) -> None:
    _exhaust(client)

    response = client.post(URL, json=PAYLOAD)

    assert response.status_code == 429
    assert response.json() == {"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
    assert response.headers["Retry-After"] == "20"
    assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
    assert response.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.usefixtures("limiter")
def test_limit_resets_after_window(client: TestClient, clock: FakeClock) -> None:
    _exhaust(client)
    assert client.post(URL, json=PAYLOAD).status_code == 429

    clock.advance(WINDOW)

    response = client.post(URL, json=PAYLOAD)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


@pytest.mark.usefixtures("limiter")
def test_clients_are_limited_independently(
    anon_client: TestClient, api_key_service: ApiKeyService, issued_key: IssuedApiKey
) -> None:
    other = api_key_service.create_key("other-client")
    _exhaust(anon_client, _bearer(issued_key))

    assert anon_client.post(URL, json=PAYLOAD, headers=_bearer(issued_key)).status_code == 429
    response = anon_client.post(URL, json=PAYLOAD, headers=_bearer(other))
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


@pytest.mark.usefixtures("limiter")
def test_keys_of_the_same_client_share_one_limit(
    anon_client: TestClient, api_key_service: ApiKeyService, issued_key: IssuedApiKey
) -> None:
    second_key = api_key_service.create_key("test-client")
    _exhaust(anon_client, _bearer(issued_key))

    assert anon_client.post(URL, json=PAYLOAD, headers=_bearer(second_key)).status_code == 429


@pytest.mark.usefixtures("limiter")
def test_health_is_never_rate_limited(client: TestClient, anon_client: TestClient) -> None:
    _exhaust(client)
    for _ in range(LIMIT * 5):
        assert client.get("/health").status_code == 200
        assert anon_client.get("/health").status_code == 200
    assert "X-RateLimit-Limit" not in client.get("/health").headers


def test_unauthenticated_requests_do_not_consume_any_bucket(
    anon_client: TestClient, limiter: InMemoryRateLimiter, auth_headers: dict[str, str]
) -> None:
    for headers in [{}, {"Authorization": "Bearer gw_live_invalid_test_key"}] * 10:
        assert anon_client.post(URL, json=PAYLOAD, headers=headers).status_code == 401

    assert limiter.tracked_clients() == 0
    _exhaust(anon_client, auth_headers)


@pytest.mark.usefixtures("limiter")
def test_rate_limit_is_checked_before_body_validation(client: TestClient) -> None:
    _exhaust(client)
    response = client.post(URL, json={"model": "", "messages": []})
    assert response.status_code == 429


@pytest.mark.usefixtures("limiter")
def test_rate_limited_requests_do_not_reach_provider(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _exhaust(client)
    calls: list[object] = []
    monkeypatch.setattr("gateway.providers.mock.MockProvider.generate", lambda self, req: calls.append(req))

    assert client.post(URL, json=PAYLOAD).status_code == 429
    assert calls == []


def test_state_is_keyed_by_client_id_not_raw_key(
    client: TestClient, limiter: InMemoryRateLimiter, issued_key: IssuedApiKey
) -> None:
    client.post(URL, json=PAYLOAD)

    assert set(limiter._buckets) == {"test-client"}
    secret_part = issued_key.api_key.rsplit("_", 1)[-1]
    assert secret_part not in repr(limiter._buckets)


@pytest.mark.usefixtures("limiter")
def test_raw_key_never_in_rate_limit_logs_or_response(
    client: TestClient, issued_key: IssuedApiKey, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _exhaust(client)

    response = client.post(URL, json=PAYLOAD)

    assert response.status_code == 429
    assert "Rate limit exceeded for client test-client" in caplog.text
    secret_part = issued_key.api_key.rsplit("_", 1)[-1]
    assert secret_part not in caplog.text
    assert issued_key.api_key not in response.text
    assert issued_key.api_key not in str(response.headers)


def test_app_builds_limiter_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.config import get_settings
    from gateway.main import create_app

    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "7")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "30")
    get_settings.cache_clear()
    try:
        limiter = create_app().state.rate_limiter
    finally:
        get_settings.cache_clear()

    assert isinstance(limiter, InMemoryRateLimiter)
    assert limiter._limit == 7
    assert limiter._window == 30
