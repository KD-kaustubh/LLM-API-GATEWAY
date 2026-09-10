"""Security headers, trusted hosts, CORS, production surface, and safe errors."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.middleware import MAX_REQUEST_BODY_BYTES, SECURITY_HEADERS

URL = "/v1/chat/completions"
PAYLOAD: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hi"}]}


@pytest.fixture
def app_with_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a fresh app (without startup) from the given environment variables."""

    def build(**env: str | None) -> Any:
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return create_app()

    yield build
    get_settings.cache_clear()


# --- security headers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/health", {}),
        ("post", URL, {"json": PAYLOAD}),
        ("get", "/nope", {}),
        ("post", URL, {"json": {"model": ""}}),
    ],
)
def test_security_headers_on_every_response(anon_client: TestClient, method: str, path: str, kwargs: dict[str, Any]) -> None:
    response = getattr(anon_client, method)(path, **kwargs)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_successful_completion_is_not_cacheable_by_intermediaries(client: TestClient) -> None:
    response = client.post(URL, json=PAYLOAD)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_no_server_banner_from_app(anon_client: TestClient) -> None:
    assert "server" not in anon_client.get("/health").headers


# --- trusted hosts -----------------------------------------------------------------


def test_untrusted_host_rejected(app_with_env: Any) -> None:
    app = app_with_env(TRUSTED_HOSTS="api.example.com")
    client = TestClient(app, base_url="http://evil.example.net")

    response = client.get("/health")

    assert response.status_code == 400
    assert response.json() == {"error": {"type": "invalid_host", "message": "Invalid host header"}}
    assert "X-Request-ID" in response.headers


@pytest.mark.parametrize(
    "host", ["api.example.com", "api.example.com:8443", "localhost:8000", "127.0.0.1", "[::1]:8000", "[::1]"]
)
def test_configured_and_loopback_hosts_allowed(app_with_env: Any, host: str) -> None:
    app = app_with_env(TRUSTED_HOSTS="api.example.com")
    assert TestClient(app).get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["", "api.example.com.evil.net", "evil.net:80", "[::2]:8000"])
def test_other_host_headers_rejected(app_with_env: Any, host: str) -> None:
    app = app_with_env(TRUSTED_HOSTS="api.example.com")
    assert TestClient(app).get("/health", headers={"Host": host}).status_code == 400


def test_wildcard_subdomain_hosts(app_with_env: Any) -> None:
    app = app_with_env(TRUSTED_HOSTS="*.example.com")
    assert TestClient(app, base_url="http://eu.example.com").get("/health").status_code == 200
    assert TestClient(app, base_url="http://example.com.evil.net").get("/health").status_code == 400


def test_default_trusted_hosts_are_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    assert Settings(_env_file=None).trusted_host_list == ["localhost", "127.0.0.1"]


# --- CORS ----------------------------------------------------------------------------


def test_cors_disabled_by_default(anon_client: TestClient) -> None:
    response = anon_client.options(
        URL, headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_configured_origins(app_with_env: Any) -> None:
    client = TestClient(app_with_env(CORS_ORIGINS="https://app.example.com"))
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}

    allowed = client.options(URL, headers={"Origin": "https://app.example.com", **preflight})
    denied = client.options(URL, headers={"Origin": "https://evil.example.net", **preflight})

    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize("origins", ["*", "https://ok.example.com,*", "app.example.com", "ftp://x.example.com"])
def test_wildcard_or_malformed_cors_origins_rejected(monkeypatch: pytest.MonkeyPatch, origins: str) -> None:
    monkeypatch.setenv("CORS_ORIGINS", origins)
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(_env_file=None)


# --- production surface ---------------------------------------------------------------


def test_docs_and_openapi_disabled_in_production(app_with_env: Any) -> None:
    client = TestClient(app_with_env(APP_ENV="production"))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_available_in_development(anon_client: TestClient) -> None:
    assert anon_client.get("/openapi.json").status_code == 200


def test_metrics_hidden_in_production_without_token(app_with_env: Any) -> None:
    client = TestClient(app_with_env(APP_ENV="production", METRICS_TOKEN=None))
    assert client.get("/metrics").status_code == 404


# --- configuration validation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [("LOG_LEVEL", "LOUD"), ("METRICS_TOKEN", "too-short")],
)
def test_invalid_hardening_settings_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None)


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_metrics_token_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "metrics-token-value-for-tests-only-0123456789"
    monkeypatch.setenv("METRICS_TOKEN", token)
    assert token not in repr(Settings(_env_file=None))


# --- error handling and existing limits -----------------------------------------------


def test_error_responses_expose_no_internals(client: TestClient) -> None:
    responses = [
        client.post(URL, json={**PAYLOAD, "model": "groq"}),
        client.post(URL, json={**PAYLOAD, "model": "unknown"}),
        client.post(URL, content=b"{not json", headers={"Content-Type": "application/json"}),
        client.get("/v1/unknown"),
    ]
    for response in responses:
        assert set(response.json()) == {"error"}
        for leaked in ("Traceback", "File \"", "site-packages", "sqlite", "SELECT", "GROQ_API_KEY=", "/app/"):
            assert leaked not in response.text


def test_body_limit_still_enforced_with_headers(client: TestClient) -> None:
    response = client.post(URL, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.headers["X-Content-Type-Options"] == "nosniff"
