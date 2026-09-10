import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

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
    return Settings(_env_file=None, groq_api_key=None, google_api_key=None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
