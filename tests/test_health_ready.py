"""Liveness (/health) vs readiness (/ready) semantics."""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.errors import TransientProviderError
from gateway.providers.mock import MockProvider
from tests.conftest import Gateway


def test_health_is_public_and_needs_no_startup(anon_client: TestClient) -> None:
    # The shared test app never ran its startup (no database), yet liveness still answers.
    response = anon_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "llm-api-gateway", "version": "0.1.0"}


def test_health_does_no_database_or_provider_work(anon_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/health must not touch the database or providers")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(MockProvider, "generate", forbidden)
    assert anon_client.get("/health").status_code == 200


def test_ready_is_503_before_startup(anon_client: TestClient) -> None:
    response = anon_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"startup": "fail", "database": "fail", "authentication": "fail"},
    }


def test_ready_after_startup(gateway: Gateway) -> None:
    client = gateway.start()
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"startup": "ok", "database": "ok", "authentication": "ok"},
    }


def test_ready_is_public(gateway: Gateway) -> None:
    client = gateway.start()
    assert client.get("/ready", headers={"Authorization": "Bearer gw_live_invalid_test_key"}).status_code == 200


def test_ready_fails_without_pepper_but_health_passes(make_gateway: Any) -> None:
    client = make_gateway("nopepper.db", API_KEY_PEPPER=None).start()

    ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json()["checks"] == {"startup": "ok", "database": "ok", "authentication": "fail"}
    assert client.get("/health").status_code == 200


def test_ready_reflects_database_loss(gateway: Gateway) -> None:
    client = gateway.start()
    assert client.get("/ready").status_code == 200
    with sqlite3.connect(gateway.db_path) as conn:
        conn.execute("DROP TABLE schema_migrations")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "fail"
    assert client.get("/health").status_code == 200


def test_ready_does_not_recreate_a_deleted_database(gateway: Gateway) -> None:
    client = gateway.start()
    for path in gateway.db_path.parent.glob(gateway.db_path.name + "*"):
        path.unlink()

    assert client.get("/ready").status_code == 503
    assert not gateway.db_path.exists()


def test_provider_outage_affects_neither_health_nor_ready(gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    def outage(self: MockProvider, request: Any) -> Any:
        raise TransientProviderError("Mock connection failed")

    monkeypatch.setattr(MockProvider, "generate", outage)
    client = gateway.start()
    issued = client.app.state.api_key_service.create_key("it-client")

    completion = client.post(
        "/v1/chat/completions",
        json={"model": "mock", "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {issued.api_key}"},
    )

    assert completion.status_code == 502
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_ready_response_has_no_internal_details(make_gateway: Any, tmp_path: Any) -> None:
    client = make_gateway("details.db", API_KEY_PEPPER=None).start()
    text = client.get("/ready").text
    for leaked in (str(tmp_path), "sqlite", "details.db", "API_KEY_PEPPER"):
        assert leaked not in text
