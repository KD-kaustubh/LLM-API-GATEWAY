"""End-to-end tests against the real app: lifespan startup, SQLite stores, cache, and usage."""

import secrets
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.config import get_settings
from gateway.main import create_app
from gateway.persistence.database import DatabaseInitializationError
from gateway.providers.mock import MockProvider
from tests.conftest import secret_of

URL = "/v1/chat/completions"
CACHEABLE: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hello cache"}], "temperature": 0}


class Gateway:
    """Boots the real app from environment settings, like `uvicorn gateway.main:app`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, db_path: Path, env: dict[str, str | None]) -> None:
        self.db_path = db_path
        self.pepper = secrets.token_urlsafe(32)
        self._monkeypatch = monkeypatch
        self._clients: list[TestClient] = []
        self._env: dict[str, str | None] = {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "API_KEY_PEPPER": self.pepper,
            "CACHE_ENABLED": "true",
            "RATE_LIMIT_REQUESTS": "1000",
            **env,
        }

    def start(self) -> TestClient:
        for name, value in self._env.items():
            if value is None:
                self._monkeypatch.delenv(name, raising=False)
            else:
                self._monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        client = TestClient(create_app())
        client.__enter__()  # runs lifespan startup; raises if startup fails
        self._clients.append(client)
        return client

    def stop(self, client: TestClient) -> None:
        self._clients.remove(client)
        client.__exit__(None, None, None)

    def close(self) -> None:
        for client in self._clients:
            client.__exit__(None, None, None)
        get_settings.cache_clear()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def file_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in self.db_path.parent.glob(self.db_path.name + "*"))


@pytest.fixture
def make_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    created: list[Gateway] = []

    def factory(name: str = "it.db", **env: str | None) -> Gateway:
        gw = Gateway(monkeypatch, tmp_path / name, env)
        created.append(gw)
        return gw

    yield factory
    for gw in created:
        gw.close()


@pytest.fixture
def gateway(make_gateway: Any) -> Gateway:
    return make_gateway()


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
