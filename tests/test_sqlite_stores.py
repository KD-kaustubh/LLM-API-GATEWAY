import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.auth.hashing import ApiKeyHasher
from gateway.auth.models import ApiKeyRecord
from gateway.auth.service import ApiKeyService
from gateway.errors import AuthenticationError
from gateway.persistence.database import Database
from gateway.persistence.migrations import initialize_database
from gateway.persistence.repositories import SQLiteApiKeyStore, SQLiteUsageRepository
from gateway.services.usage import UsageRecord
from tests.conftest import secret_of

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(key_id: str = "0123456789abcdef", client_id: str = "client-a") -> ApiKeyRecord:
    return ApiKeyRecord(key_id=key_id, key_hash="ab" * 32, client_id=client_id, created_at=NOW)


def _db_bytes(db: Database) -> bytes:
    return b"".join(p.read_bytes() for p in db.path.parent.glob(db.path.name + "*"))


def _reopen(db: Database) -> Database:
    return initialize_database(f"sqlite:///{db.path.as_posix()}")


# --- SQLiteApiKeyStore -------------------------------------------------------


def test_add_and_get_round_trip(database: Database) -> None:
    store = SQLiteApiKeyStore(database)
    store.add(_record())
    assert store.get("0123456789abcdef") == _record()


def test_get_unknown_returns_none(database: Database) -> None:
    assert SQLiteApiKeyStore(database).get("ffffffffffffffff") is None


def test_duplicate_key_id_rejected(database: Database) -> None:
    store = SQLiteApiKeyStore(database)
    store.add(_record())
    with pytest.raises(ValueError, match="Duplicate"):
        store.add(_record())


def test_revoke_persists_first_timestamp(database: Database) -> None:
    store = SQLiteApiKeyStore(database)
    store.add(_record())

    assert store.revoke("0123456789abcdef", NOW + timedelta(days=1))
    assert store.revoke("0123456789abcdef", NOW + timedelta(days=2))

    assert store.get("0123456789abcdef").revoked_at == NOW + timedelta(days=1)  # type: ignore[union-attr]


def test_revoke_unknown_returns_false(database: Database) -> None:
    assert not SQLiteApiKeyStore(database).revoke("ffffffffffffffff", NOW)


def test_service_keys_survive_restart_and_revocation_persists(database: Database) -> None:
    pepper = secrets.token_bytes(32)
    service = ApiKeyService(SQLiteApiKeyStore(database), ApiKeyHasher(pepper))
    issued = service.create_key("client-a")
    other = service.create_key("client-a")

    reloaded = ApiKeyService(SQLiteApiKeyStore(_reopen(database)), ApiKeyHasher(pepper))
    assert reloaded.authenticate(issued.api_key).client_id == "client-a"

    assert reloaded.revoke_key(issued.record.key_id)

    after_second_restart = ApiKeyService(SQLiteApiKeyStore(_reopen(database)), ApiKeyHasher(pepper))
    with pytest.raises(AuthenticationError):
        after_second_restart.authenticate(issued.api_key)
    assert after_second_restart.authenticate(other.api_key).key_id == other.record.key_id


def test_raw_key_and_pepper_never_written_to_database(database: Database) -> None:
    pepper = secrets.token_bytes(32)
    service = ApiKeyService(SQLiteApiKeyStore(database), ApiKeyHasher(pepper))
    issued = service.create_key("client-a")
    service.authenticate(issued.api_key)

    raw = _db_bytes(database)
    assert issued.api_key.encode() not in raw
    assert secret_of(issued.api_key).encode() not in raw
    assert pepper not in raw
    assert pepper.hex().encode() not in raw
    with database.connection() as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(api_keys)")]
    assert columns == ["key_id", "key_hash", "client_id", "created_at", "revoked_at"]


# --- SQLiteUsageRepository ---------------------------------------------------


def _usage(request_id: str, **overrides: object) -> UsageRecord:
    values: dict[str, object] = dict(
        request_id=request_id, client_id="client-a", key_id="0123456789abcdef", model="llama",
        provider="groq", created_at=NOW, input_tokens=10, output_tokens=5, total_tokens=15,
        latency_ms=42, cache_hit=False,
    )
    values.update(overrides)
    return UsageRecord(**values)  # type: ignore[arg-type]


def test_usage_round_trip(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    record = _usage("req_1")
    repo.record(record)
    assert repo.list_for_client("client-a") == [record]


def test_usage_null_tokens_stay_null(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    repo.record(_usage("req_1", input_tokens=None, output_tokens=None, total_tokens=None))
    [stored] = repo.list_for_client("client-a")
    assert (stored.input_tokens, stored.output_tokens, stored.total_tokens) == (None, None, None)


def test_usage_filtered_by_client_and_ordered_newest_first(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    repo.record(_usage("req_old", created_at=NOW))
    repo.record(_usage("req_new", created_at=NOW + timedelta(minutes=1)))
    repo.record(_usage("req_other", client_id="client-b"))

    assert [r.request_id for r in repo.list_for_client("client-a")] == ["req_new", "req_old"]


def test_usage_request_id_is_unique(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    repo.record(_usage("req_1"))
    with pytest.raises(Exception):
        repo.record(_usage("req_1"))


def test_usage_table_has_no_secret_columns(database: Database) -> None:
    with database.connection() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
    assert columns == {
        "id", "request_id", "client_id", "key_id", "model", "provider", "created_at",
        "input_tokens", "output_tokens", "total_tokens", "latency_ms", "cache_hit",
    }


def test_tmp_database_is_isolated(database: Database, tmp_path: Path) -> None:
    assert database.path.parent == tmp_path
    assert "data" not in database.path.parts
