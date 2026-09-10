import secrets
import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.api.security import get_api_key_service
from gateway.auth.hashing import ApiKeyHasher
from gateway.auth.models import IssuedApiKey
from gateway.auth.service import ApiKeyService
from gateway.auth.store import InMemoryApiKeyStore
from gateway.config import Settings, get_settings
from gateway.main import app

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect


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
        api_key_hashes=None,
    )


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


@pytest.fixture
def anon_client(settings: Settings, api_key_service: ApiKeyService) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_api_key_service] = lambda: api_key_service
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client: TestClient, auth_headers: dict[str, str]) -> TestClient:
    """Authenticated client; shares anon_client's dependency overrides."""
    return TestClient(app, headers=auth_headers)
