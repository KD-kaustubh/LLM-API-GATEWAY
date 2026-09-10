import logging
import os
import secrets
import shutil
import socket
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Safety net: anything that builds the real app during tests uses a throwaway database,
# never the development database. Set before any gateway module reads settings.
_SESSION_DB_DIR = Path(tempfile.mkdtemp(prefix="gateway-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_SESSION_DB_DIR / 'session.db').as_posix()}"
# Starlette's TestClient sends Host: testserver.
os.environ["TRUSTED_HOSTS"] = "testserver"

from fastapi.testclient import TestClient  # noqa: E402

from gateway.api.routes import get_cache_store, get_usage_recorder  # noqa: E402
from gateway.api.security import get_api_key_service, get_rate_limiter  # noqa: E402
from gateway.auth.hashing import ApiKeyHasher  # noqa: E402
from gateway.auth.models import IssuedApiKey  # noqa: E402
from gateway.auth.service import ApiKeyService  # noqa: E402
from gateway.auth.store import InMemoryApiKeyStore  # noqa: E402
from gateway.config import Settings, get_settings  # noqa: E402
from gateway.main import app, create_app  # noqa: E402
from gateway.persistence.database import Database  # noqa: E402
from gateway.persistence.migrations import initialize_database  # noqa: E402
from gateway.rate_limit import InMemoryRateLimiter  # noqa: E402
from gateway.services.cache import InMemoryCacheStore  # noqa: E402
from gateway.services.usage import InMemoryUsageRecorder  # noqa: E402


def secret_of(api_key: str) -> str:
    """The 43-char secret of gw_live_<16 hex>_<secret>. Fixed offset: the secret may contain '_'."""
    return api_key[len("gw_live_") + 17:]


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_SESSION_DB_DIR, ignore_errors=True)


def _guarded_connect(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(f"Tests must not make network calls (attempted: {host})")
    _real_connect(self, address)


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        groq_api_key=None,
        google_api_key=None,
        api_key_pepper=None,
        cache_enabled=False,
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """A fresh, migrated SQLite database per test."""
    return initialize_database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


@pytest.fixture
def key_store() -> InMemoryApiKeyStore:
    return InMemoryApiKeyStore()


@pytest.fixture
def api_key_service(key_store: InMemoryApiKeyStore) -> ApiKeyService:
    return ApiKeyService(key_store, ApiKeyHasher(secrets.token_bytes(32)))


@pytest.fixture
def issued_key(api_key_service: ApiKeyService) -> IssuedApiKey:
    return api_key_service.create_key("test-client")


@pytest.fixture
def auth_headers(issued_key: IssuedApiKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued_key.api_key}"}


class FakeClock:
    """Manually advanced clock for deterministic time-based tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rate_limiter(clock: FakeClock) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(limit=1000, window_seconds=60, clock=clock)


@pytest.fixture
def usage_recorder() -> InMemoryUsageRecorder:
    return InMemoryUsageRecorder()


@pytest.fixture
def cache_store() -> InMemoryCacheStore:
    return InMemoryCacheStore(max_entries=100)


@pytest.fixture
def anon_client(
    settings: Settings,
    api_key_service: ApiKeyService,
    rate_limiter: InMemoryRateLimiter,
    usage_recorder: InMemoryUsageRecorder,
    cache_store: InMemoryCacheStore,
) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_api_key_service] = lambda: api_key_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    app.dependency_overrides[get_usage_recorder] = lambda: usage_recorder
    app.dependency_overrides[get_cache_store] = lambda: cache_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client: TestClient, auth_headers: dict[str, str]) -> TestClient:
    """Authenticated client; shares anon_client's dependency overrides."""
    return TestClient(app, headers=auth_headers)


# --- real-app harness (lifespan startup + SQLite), shared by integration tests ---


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
        # Startup attached a JSON handler bound to this test's captured stdout; detach it.
        gateway_logger = logging.getLogger("gateway")
        for handler in [h for h in gateway_logger.handlers if h.get_name() == "gateway-json"]:
            gateway_logger.removeHandler(handler)
        gateway_logger.setLevel(logging.NOTSET)

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
