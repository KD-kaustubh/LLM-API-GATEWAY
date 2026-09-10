import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.persistence.database import (
    Database,
    DatabaseInitializationError,
    sqlite_path_from_url,
)
from gateway.persistence.migrations import LATEST_VERSION, initialize_database, migrate
from gateway.persistence.repositories import SQLiteUsageRepository
from gateway.services.usage import UsageRecord


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _tables(db: Database) -> set[str]:
    with db.connection() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_columns(db: Database, table: str) -> dict[str, list[str]]:
    with db.connection() as conn:
        indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()  # table names are constants
        return {
            idx[1]: [col[2] for col in conn.execute(f"PRAGMA index_info('{idx[1]}')")] for idx in indexes
        }


def _usage(request_id: str, client_id: str = "client-a") -> UsageRecord:
    return UsageRecord(
        request_id=request_id, client_id=client_id, key_id="0123456789abcdef", model="mock",
        provider="mock", created_at=datetime.now(timezone.utc), input_tokens=1, output_tokens=2,
        total_tokens=3, latency_ms=5, cache_hit=False,
    )


def test_initialize_creates_file_and_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "gateway.db"
    db = initialize_database(_url(path))
    assert path.exists()
    assert db.path == path


def test_schema_tables_exist(database: Database) -> None:
    assert {"api_keys", "usage_records", "cache_entries", "schema_migrations"} <= _tables(database)


def test_expected_indexes_exist(database: Database) -> None:
    api_keys = _index_columns(database, "api_keys")
    usage = _index_columns(database, "usage_records")
    cache = _index_columns(database, "cache_entries")

    assert ["key_id"] in api_keys.values()  # primary-key index
    assert ["client_id"] in api_keys.values()
    assert any(cols[0] == "client_id" for cols in usage.values())
    assert ["created_at"] in usage.values()
    assert ["request_id"] in usage.values()  # unique constraint
    assert ["expires_at"] in cache.values()
    assert ["cache_key"] in cache.values()


def test_wal_mode_enabled(database: Database) -> None:
    with database.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_initialization_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    url = _url(tmp_path / "g.db")
    db = initialize_database(url)
    SQLiteUsageRepository(db).record(_usage("req_1"))

    for _ in range(3):
        db = initialize_database(url)

    assert [r.request_id for r in SQLiteUsageRepository(db).list_for_client("client-a")] == ["req_1"]
    with db.connection() as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [LATEST_VERSION]


def test_migrate_returns_latest_version(database: Database) -> None:
    assert migrate(database) == LATEST_VERSION


def test_newer_schema_version_is_refused(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (LATEST_VERSION + 1, "x"))

    with pytest.raises(DatabaseInitializationError, match="newer than supported"):
        initialize_database(_url(database.path))


def test_corrupt_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"definitely not sqlite" * 200)

    with pytest.raises(DatabaseInitializationError, match="Could not initialize database"):
        initialize_database(_url(path))


def test_directory_as_database_path_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DatabaseInitializationError):
        initialize_database(_url(tmp_path))


@pytest.mark.parametrize(
    "url", ["postgresql://user@host/db", "sqlite://relative.db", "sqlite:///", "sqlite:///:memory:", "gateway.db"]
)
def test_invalid_database_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        sqlite_path_from_url(url)
    with pytest.raises(DatabaseInitializationError):
        initialize_database(url)


def test_url_parsing() -> None:
    assert sqlite_path_from_url("sqlite:///./data/gateway.db") == Path("./data/gateway.db")
    assert sqlite_path_from_url("sqlite:////var/lib/gateway.db") == Path("/var/lib/gateway.db")


def test_failed_transaction_rolls_back(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    with pytest.raises(sqlite3.IntegrityError):
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO usage_records (request_id, client_id, model, provider, created_at, latency_ms, cache_hit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("req_a", "client-a", "m", "p", "t", 1, 0),
            )
            conn.execute(
                "INSERT INTO usage_records (request_id, client_id, model, provider, created_at, latency_ms, cache_hit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("req_a", "client-a", "m", "p", "t", 1, 0),
            )
    assert repo.list_for_client("client-a") == []


def test_values_are_parameterized_not_interpolated(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    hostile = "x'); DROP TABLE usage_records; --"
    repo.record(_usage("req_hostile", client_id=hostile))

    [stored] = repo.list_for_client(hostile)
    assert stored.client_id == hostile
    assert "usage_records" in _tables(database)


def test_concurrent_writes_do_not_lose_or_corrupt_rows(database: Database) -> None:
    repo = SQLiteUsageRepository(database)
    threads, per_thread = 8, 25
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        barrier.wait()
        try:
            for i in range(per_thread):
                repo.record(_usage(f"req_{n}_{i}", client_id="shared"))
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert errors == []
    rows = repo.list_for_client("shared", limit=10_000)
    assert len(rows) == threads * per_thread
    assert len({r.request_id for r in rows}) == threads * per_thread
    with database.connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_initialization_is_safe(tmp_path: Path) -> None:
    url = _url(tmp_path / "race.db")
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def start() -> None:
        barrier.wait()
        try:
            initialize_database(url)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    workers = [threading.Thread(target=start) for _ in range(6)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert errors == []
    with Database(tmp_path / "race.db").connection() as conn:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [LATEST_VERSION]
