"""End-to-end tests against the real app: lifespan startup, SQLite stores, cache, and usage."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.config import get_settings
from gateway.main import create_app
from gateway.persistence.database import DatabaseInitializationError
from gateway.providers.mock import MockProvider
from tests.conftest import Gateway, secret_of

URL = "/v1/chat/completions"
CACHEABLE: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hello cache"}], "temperature": 0}


@pytest.fixture
def provider_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = MockProvider.generate

    def counting(self: MockProvider, request: Any) -> Any:
        calls.append(1)
        return original(self, request)

    monkeypatch.setattr(MockProvider, "generate", counting)
    return calls


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_full_flow_cache_and_usage(gateway: Gateway, provider_calls: list[int]) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    headers = _bearer(issued.api_key)

    assert client.get("/health").status_code == 200

    first = client.post(URL, json=CACHEABLE, headers=headers)
    second = client.post(URL, json=CACHEABLE, headers=headers)
    different = client.post(URL, json={**CACHEABLE, "max_tokens": 9}, headers=headers)
    uncacheable = client.post(URL, json={**CACHEABLE, "temperature": 0.5}, headers=headers)

    assert [r.status_code for r in (first, second, different, uncacheable)] == [200] * 4
    assert [r.headers["X-Cache"] for r in (first, second, different, uncacheable)] == ["MISS", "HIT", "MISS", "BYPASS"]
    assert len(provider_calls) == 3
    assert second.json()["content"] == first.json()["content"]
    assert second.json()["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    usage = gateway.rows(
        "SELECT request_id, client_id, key_id, provider, cache_hit, total_tokens FROM usage_records ORDER BY id"
    )
    assert [u[0] for u in usage] == [r.json()["id"] for r in (first, second, different, uncacheable)]
    assert {(u[1], u[2]) for u in usage} == {("it-client", issued.record.key_id)}
    assert [(u[3], u[4]) for u in usage] == [("mock", 0), ("cache", 1), ("mock", 0), ("mock", 0)]
    assert usage[1][5] == 0
    assert usage[0][5] == first.json()["usage"]["total_tokens"]
    assert gateway.rows("SELECT COUNT(*) FROM cache_entries") == [(2,)]


def test_database_contains_no_secrets(gateway: Gateway) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key))
    client.post(URL, json=CACHEABLE, headers=_bearer("gw_live_invalid_test_key"))

    raw = gateway.file_bytes()
    assert issued.api_key.encode() not in raw
    assert secret_of(issued.api_key).encode() not in raw
    assert gateway.pepper.encode() not in raw
    assert b"Bearer" not in raw
    assert b"gw_live_" not in raw
    [(stored_hash,)] = gateway.rows("SELECT key_hash FROM api_keys")
    assert len(stored_hash) == 64


def test_keys_survive_restart_and_revocation_persists(gateway: Gateway) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    gateway.stop(client)

    restarted = gateway.start()
    assert restarted.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)).status_code == 200

    restarted.app.state.api_key_service.revoke_key(issued.record.key_id)
    gateway.stop(restarted)

    after = gateway.start()
    response = after.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key))
    assert response.status_code == 401
    assert after.get("/health").status_code == 200


def test_cache_persists_across_restart(gateway: Gateway, provider_calls: list[int]) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    assert client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)).headers["X-Cache"] == "MISS"
    gateway.stop(client)

    restarted = gateway.start()
    assert restarted.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)).headers["X-Cache"] == "HIT"
    assert len(provider_calls) == 1


def test_cache_disabled_by_default(make_gateway: Any, provider_calls: list[int]) -> None:
    gateway = make_gateway("default.db", CACHE_ENABLED=None)
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")

    responses = [client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)) for _ in range(2)]

    assert [r.headers["X-Cache"] for r in responses] == ["BYPASS", "BYPASS"]
    assert len(provider_calls) == 2
    assert gateway.rows("SELECT COUNT(*) FROM cache_entries") == [(0,)]


def test_cache_hit_still_consumes_rate_limit(make_gateway: Any) -> None:
    gateway = make_gateway("rl.db", RATE_LIMIT_REQUESTS="3")
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")

    responses = [client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)) for _ in range(4)]

    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert [r.headers.get("X-Cache") for r in responses[:3]] == ["MISS", "HIT", "HIT"]
    assert [r.headers["X-RateLimit-Remaining"] for r in responses[:3]] == ["2", "1", "0"]
    assert gateway.rows("SELECT COUNT(*) FROM usage_records") == [(3,)]


def test_rejected_requests_create_no_usage_or_cache(gateway: Gateway, provider_calls: list[int]) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")

    assert client.post(URL, json=CACHEABLE).status_code == 401
    assert client.post(URL, json=CACHEABLE, headers=_bearer("gw_live_invalid_test_key")).status_code == 401
    assert client.post(URL, json={**CACHEABLE, "messages": []}, headers=_bearer(issued.api_key)).status_code == 422
    assert client.post(URL, json={**CACHEABLE, "model": "groq"}, headers=_bearer(issued.api_key)).status_code == 503

    assert provider_calls == []
    assert gateway.rows("SELECT COUNT(*) FROM usage_records") == [(0,)]
    assert gateway.rows("SELECT COUNT(*) FROM cache_entries") == [(0,)]


def test_provider_failure_creates_no_usage_or_cache(gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.errors import ProviderError

    def failing(self: MockProvider, request: Any) -> Any:
        raise ProviderError("Mock request failed with status 400")

    monkeypatch.setattr(MockProvider, "generate", failing)
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")

    response = client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key))

    assert response.status_code == 502
    assert gateway.rows("SELECT COUNT(*) FROM usage_records") == [(0,)]
    assert gateway.rows("SELECT COUNT(*) FROM cache_entries") == [(0,)]


def test_usage_write_failure_fails_open(gateway: Gateway) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    with sqlite3.connect(gateway.db_path) as conn:
        conn.execute("DROP TABLE usage_records")

    response = client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key))

    assert response.status_code == 200
    assert "usage_records" not in response.text


def test_cache_failure_fails_open(gateway: Gateway) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    with sqlite3.connect(gateway.db_path) as conn:
        conn.execute("DROP TABLE cache_entries")

    responses = [client.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key)) for _ in range(2)]

    assert [r.status_code for r in responses] == [200, 200]
    assert [r.headers["X-Cache"] for r in responses] == ["MISS", "MISS"]


def test_credential_store_failure_fails_closed(gateway: Gateway, provider_calls: list[int]) -> None:
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")
    with sqlite3.connect(gateway.db_path) as conn:
        conn.execute("DROP TABLE api_keys")
    lenient = TestClient(client.app, raise_server_exceptions=False)

    response = lenient.post(URL, json=CACHEABLE, headers=_bearer(issued.api_key))

    assert response.status_code == 500
    assert response.json() == {"error": {"type": "internal_error", "message": "An unexpected error occurred"}}
    assert provider_calls == []


def test_startup_fails_closed_on_corrupt_database(make_gateway: Any, tmp_path: Path) -> None:
    (tmp_path / "corrupt.db").write_bytes(b"not a database" * 500)
    gateway = make_gateway("corrupt.db")

    with pytest.raises(DatabaseInitializationError):
        gateway.start()


def test_production_without_pepper_fails_startup(make_gateway: Any) -> None:
    gateway = make_gateway("prod.db", APP_ENV="production", API_KEY_PEPPER=None)

    with pytest.raises(ValueError, match="API_KEY_PEPPER"):
        gateway.start()


def test_creating_app_does_not_touch_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "lazy" / "gateway.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    try:
        create_app()
    finally:
        get_settings.cache_clear()

    assert not db_path.parent.exists()
