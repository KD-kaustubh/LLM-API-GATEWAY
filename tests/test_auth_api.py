from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from gateway.api.security import authenticate_request
from gateway.auth.keys import generate_api_key
from gateway.auth.models import AuthenticatedClient, IssuedApiKey
from gateway.auth.service import ApiKeyService
from gateway.errors import AuthenticationError
from gateway.middleware import MAX_REQUEST_BODY_BYTES

URL = "/v1/chat/completions"
PAYLOAD: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hi"}]}
AUTH_ERROR = {"error": {"type": "authentication_error", "message": "Invalid API key"}}


def _assert_auth_error(response: Any) -> None:
    assert response.status_code == 401
    assert response.json() == AUTH_ERROR
    assert response.headers["www-authenticate"] == "Bearer"


# --- dependency --------------------------------------------------------------


def _request_stub() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def test_dependency_returns_authenticated_client(
    api_key_service: ApiKeyService, issued_key: IssuedApiKey
) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=issued_key.api_key)
    request = _request_stub()

    client = authenticate_request(request, credentials, api_key_service)

    assert client == AuthenticatedClient(client_id="test-client", key_id=issued_key.record.key_id)
    # Only safe identifiers are exposed to the access log, never the raw key.
    assert vars(request.state) == {"client_id": "test-client", "key_id": issued_key.record.key_id}


def test_dependency_rejects_missing_credentials(api_key_service: ApiKeyService) -> None:
    request = _request_stub()
    with pytest.raises(AuthenticationError):
        authenticate_request(request, None, api_key_service)
    assert vars(request.state) == {}


# --- public / protected endpoints ---------------------------------------------


def test_health_is_public(anon_client: TestClient) -> None:
    response = anon_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_ignores_invalid_credentials(anon_client: TestClient) -> None:
    response = anon_client.get("/health", headers={"Authorization": "Bearer gw_live_invalid_test_key"})
    assert response.status_code == 200


def test_valid_key_allows_completion(anon_client: TestClient, auth_headers: dict[str, str]) -> None:
    response = anon_client.post(URL, json=PAYLOAD, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["provider"] == "mock"


def test_missing_authorization_rejected(anon_client: TestClient) -> None:
    _assert_auth_error(anon_client.post(URL, json=PAYLOAD))


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("", id="empty"),
        pytest.param("Bearer", id="scheme-only"),
        pytest.param("Bearer ", id="scheme-with-space"),
        pytest.param("gw_live_invalid_test_key", id="no-scheme"),
        pytest.param("Basic dXNlcjpwYXNz", id="basic-scheme"),
        pytest.param("Token gw_live_invalid_test_key", id="token-scheme"),
        pytest.param("Bearer gw_live_invalid_test_key", id="malformed-key"),
        pytest.param("Bearer gw_live_invalid_test_key extra", id="extra-parts"),
    ],
)
def test_malformed_authorization_rejected(anon_client: TestClient, header: str) -> None:
    _assert_auth_error(anon_client.post(URL, json=PAYLOAD, headers={"Authorization": header}))


def test_wrong_scheme_with_valid_key_rejected(anon_client: TestClient, issued_key: IssuedApiKey) -> None:
    response = anon_client.post(URL, json=PAYLOAD, headers={"Authorization": f"Basic {issued_key.api_key}"})
    _assert_auth_error(response)


def test_lowercase_bearer_scheme_accepted(anon_client: TestClient, issued_key: IssuedApiKey) -> None:
    response = anon_client.post(URL, json=PAYLOAD, headers={"Authorization": f"bearer {issued_key.api_key}"})
    assert response.status_code == 200


def test_unknown_key_rejected(anon_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {generate_api_key()}"}
    _assert_auth_error(anon_client.post(URL, json=PAYLOAD, headers=headers))


def test_revoked_key_rejected(
    anon_client: TestClient,
    api_key_service: ApiKeyService,
    issued_key: IssuedApiKey,
    auth_headers: dict[str, str],
) -> None:
    assert anon_client.post(URL, json=PAYLOAD, headers=auth_headers).status_code == 200

    api_key_service.revoke_key(issued_key.record.key_id)

    _assert_auth_error(anon_client.post(URL, json=PAYLOAD, headers=auth_headers))


def test_auth_runs_before_body_validation(anon_client: TestClient) -> None:
    _assert_auth_error(anon_client.post(URL, json={"model": "", "messages": []}))


# --- secret handling in responses ---------------------------------------------


def test_raw_key_not_in_success_response(client: TestClient, issued_key: IssuedApiKey) -> None:
    response = client.post(URL, json=PAYLOAD)
    assert issued_key.api_key not in response.text
    assert issued_key.api_key not in str(response.headers)


def test_presented_key_not_echoed_in_auth_error(anon_client: TestClient) -> None:
    presented = generate_api_key()
    response = anon_client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {presented}"})
    assert presented not in response.text
    assert presented not in str(response.headers)


def test_validation_errors_do_not_echo_secrets_or_input(
    client: TestClient, issued_key: IssuedApiKey
) -> None:
    canary = "canary-value-that-must-not-be-echoed"
    bad_payload = {
        "model": "mock",
        "messages": [{"role": canary, "content": canary}],
        "temperature": canary,
        "unexpected_field": issued_key.api_key,
    }

    response = client.post(URL, json=bad_payload)

    assert response.status_code == 422
    assert issued_key.api_key not in response.text
    assert canary not in response.text
    details = response.json()["error"]["details"]
    assert details
    for detail in details:
        assert set(detail) == {"loc", "msg", "type"}


def test_validation_details_are_bounded(client: TestClient) -> None:
    messages = [{"role": "invalid", "content": ""} for _ in range(50)]
    long_field = "f" * 5000
    response = client.post(URL, json={"model": "mock", "messages": messages, long_field: 1})

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert len(details) == 10
    assert long_field not in response.text


# --- body size limit ---------------------------------------------------------


def test_oversized_body_rejected_by_content_length(client: TestClient) -> None:
    body = b'{"model": "mock", "messages": [{"role": "user", "content": "' + b"x" * MAX_REQUEST_BODY_BYTES + b'"}]}'

    response = client.post(URL, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json() == {"error": {"type": "request_too_large", "message": "Request body too large"}}


def test_oversized_streamed_body_rejected(client: TestClient) -> None:
    chunk = b"x" * (64 * 1024)
    chunks = iter([chunk] * ((MAX_REQUEST_BODY_BYTES // len(chunk)) + 2))

    response = client.post(URL, content=chunks, headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"


def test_body_under_limit_accepted(client: TestClient) -> None:
    payload = {"model": "mock", "messages": [{"role": "user", "content": "x" * 50_000}]}
    assert client.post(URL, json=payload).status_code == 200


# --- consistent error envelope -------------------------------------------------


def test_unknown_route_uses_error_envelope(anon_client: TestClient) -> None:
    response = anon_client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"error": {"type": "not_found", "message": "Not Found"}}
