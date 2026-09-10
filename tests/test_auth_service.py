import hmac
import logging
from dataclasses import astuple

import pytest

from gateway.auth import hashing
from gateway.auth.keys import generate_api_key
from gateway.auth.models import AuthenticatedClient, IssuedApiKey
from gateway.auth.service import ApiKeyService
from gateway.auth.store import InMemoryApiKeyStore
from gateway.errors import AuthenticationError


def test_create_key_returns_raw_key_and_record(issued_key: IssuedApiKey) -> None:
    assert issued_key.api_key.startswith("gw_live_")
    assert issued_key.record.client_id == "test-client"
    assert issued_key.record.created_at.tzinfo is not None
    assert not issued_key.record.revoked


def test_raw_key_is_never_stored(key_store: InMemoryApiKeyStore, issued_key: IssuedApiKey) -> None:
    stored = key_store.get(issued_key.record.key_id)
    assert stored is not None

    secret_part = issued_key.api_key.rsplit("_", 1)[-1]
    for value in astuple(stored):
        assert issued_key.api_key not in str(value)
        assert secret_part not in str(value)
    assert secret_part not in repr(key_store._records)


def test_issued_key_repr_hides_raw_key(issued_key: IssuedApiKey) -> None:
    assert issued_key.api_key not in repr(issued_key)


def test_invalid_client_id_rejected(api_key_service: ApiKeyService) -> None:
    with pytest.raises(ValueError):
        api_key_service.create_key("bad client id!")


def test_valid_key_authenticates(api_key_service: ApiKeyService, issued_key: IssuedApiKey) -> None:
    client = api_key_service.authenticate(issued_key.api_key)
    assert client == AuthenticatedClient(client_id="test-client", key_id=issued_key.record.key_id)


@pytest.mark.parametrize(
    "presented",
    ["", "gw_live_invalid_test_key", "not-a-key"],
    ids=["empty", "malformed", "foreign-format"],
)
def test_malformed_keys_rejected(api_key_service: ApiKeyService, presented: str) -> None:
    with pytest.raises(AuthenticationError):
        api_key_service.authenticate(presented)


def test_unknown_well_formed_key_rejected(api_key_service: ApiKeyService) -> None:
    with pytest.raises(AuthenticationError):
        api_key_service.authenticate(generate_api_key())


def test_known_key_id_with_wrong_secret_rejected(
    api_key_service: ApiKeyService, issued_key: IssuedApiKey
) -> None:
    prefix, _, secret = issued_key.api_key.rpartition("_")
    tampered = f"{prefix}_{'A' if secret[0] != 'A' else 'B'}{secret[1:]}"

    with pytest.raises(AuthenticationError):
        api_key_service.authenticate(tampered)


def test_revoked_key_rejected(api_key_service: ApiKeyService, issued_key: IssuedApiKey) -> None:
    assert api_key_service.revoke_key(issued_key.record.key_id)

    with pytest.raises(AuthenticationError):
        api_key_service.authenticate(issued_key.api_key)


def test_revoking_one_key_leaves_others_valid(api_key_service: ApiKeyService) -> None:
    first = api_key_service.create_key("client-a")
    second = api_key_service.create_key("client-a")

    api_key_service.revoke_key(first.record.key_id)

    assert api_key_service.authenticate(second.api_key).key_id == second.record.key_id


def test_all_failures_raise_identical_error(
    api_key_service: ApiKeyService, issued_key: IssuedApiKey
) -> None:
    unknown = generate_api_key()
    api_key_service.revoke_key(issued_key.record.key_id)

    messages = set()
    for presented in ["gw_live_invalid_test_key", unknown, issued_key.api_key]:
        with pytest.raises(AuthenticationError) as exc_info:
            api_key_service.authenticate(presented)
        messages.add((exc_info.value.status_code, exc_info.value.message))

    assert messages == {(401, "Invalid API key")}


@pytest.mark.parametrize("known", [True, False], ids=["known-key", "unknown-key"])
def test_timing_safe_compare_runs_for_known_and_unknown_keys(
    api_key_service: ApiKeyService,
    issued_key: IssuedApiKey,
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
) -> None:
    calls = []
    real_compare = hmac.compare_digest
    monkeypatch.setattr(
        hashing.hmac, "compare_digest", lambda a, b: calls.append(1) or real_compare(a, b)
    )
    presented = issued_key.api_key if known else generate_api_key()

    try:
        api_key_service.authenticate(presented)
    except AuthenticationError:
        pass

    assert len(calls) == 1


def test_raw_keys_never_logged(
    api_key_service: ApiKeyService, issued_key: IssuedApiKey, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    unknown = generate_api_key()
    other = api_key_service.create_key("other-client")
    api_key_service.revoke_key(other.record.key_id)

    api_key_service.authenticate(issued_key.api_key)
    for presented in [unknown, other.api_key, "gw_live_invalid_test_key"]:
        with pytest.raises(AuthenticationError):
            api_key_service.authenticate(presented)

    auth_logs = [r for r in caplog.records if r.name.startswith("gateway.auth")]
    assert len(auth_logs) == 3
    for raw in (issued_key.api_key, unknown, other.api_key):
        assert raw.rsplit("_", 1)[-1] not in caplog.text
