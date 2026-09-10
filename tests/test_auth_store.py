from datetime import datetime, timezone

import pytest

from gateway.auth.models import ApiKeyRecord
from gateway.auth.store import InMemoryApiKeyStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(key_id: str = "0123456789abcdef") -> ApiKeyRecord:
    return ApiKeyRecord(key_id=key_id, key_hash="ab" * 32, client_id="client-a", created_at=NOW)


def test_add_and_get() -> None:
    store = InMemoryApiKeyStore()
    record = _record()
    store.add(record)
    assert store.get(record.key_id) == record


def test_get_unknown_returns_none() -> None:
    assert InMemoryApiKeyStore().get("ffffffffffffffff") is None


def test_duplicate_key_id_rejected() -> None:
    store = InMemoryApiKeyStore()
    store.add(_record())
    with pytest.raises(ValueError):
        store.add(_record())


def test_revoke_marks_record() -> None:
    store = InMemoryApiKeyStore()
    store.add(_record())

    assert store.revoke("0123456789abcdef", NOW)

    revoked = store.get("0123456789abcdef")
    assert revoked is not None
    assert revoked.revoked
    assert revoked.revoked_at == NOW


def test_revoke_is_idempotent_and_keeps_first_timestamp() -> None:
    store = InMemoryApiKeyStore()
    store.add(_record())
    store.revoke("0123456789abcdef", NOW)

    assert store.revoke("0123456789abcdef", datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert store.get("0123456789abcdef").revoked_at == NOW  # type: ignore[union-attr]


def test_revoke_unknown_returns_false() -> None:
    assert not InMemoryApiKeyStore().revoke("ffffffffffffffff", NOW)
