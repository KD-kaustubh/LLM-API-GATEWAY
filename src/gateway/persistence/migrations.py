import sqlite3
import time
from datetime import datetime, timezone

from gateway.persistence.database import (
    BUSY_TIMEOUT_SECONDS,
    Database,
    DatabaseInitializationError,
)

# Append-only: never edit or reorder an applied migration; add a new version instead.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE api_keys (
                key_id     TEXT PRIMARY KEY,
                key_hash   TEXT NOT NULL,
                client_id  TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            "CREATE INDEX idx_api_keys_client_id ON api_keys (client_id)",
            """
            CREATE TABLE usage_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id    TEXT NOT NULL UNIQUE,
                client_id     TEXT NOT NULL,
                key_id        TEXT,
                model         TEXT NOT NULL,
                provider      TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                input_tokens  INTEGER,
                output_tokens INTEGER,
                total_tokens  INTEGER,
                latency_ms    INTEGER NOT NULL,
                cache_hit     INTEGER NOT NULL CHECK (cache_hit IN (0, 1))
            )
            """,
            "CREATE INDEX idx_usage_records_client_id_created_at ON usage_records (client_id, created_at)",
            "CREATE INDEX idx_usage_records_created_at ON usage_records (created_at)",
            """
            CREATE TABLE cache_entries (
                cache_key        TEXT PRIMARY KEY,
                response_payload TEXT NOT NULL,
                model            TEXT NOT NULL,
                created_at       REAL NOT NULL,
                expires_at       REAL NOT NULL,
                size_bytes       INTEGER NOT NULL
            )
            """,
            "CREATE INDEX idx_cache_entries_expires_at ON cache_entries (expires_at)",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1][0]


_WAL_ATTEMPTS = 50
_WAL_RETRY_SECONDS = 0.1


def _enable_wal(conn: sqlite3.Connection) -> None:
    # Switching to WAL needs an exclusive lock, and SQLite reports "locked" immediately (without
    # the busy handler) when two connections contend for it, so retry a bounded number of times.
    for attempt in range(_WAL_ATTEMPTS):
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _WAL_ATTEMPTS - 1:
                raise
            time.sleep(_WAL_RETRY_SECONDS)


def migrate(database: Database) -> int:
    """Apply missing migrations in order and return the schema version. Never drops data."""
    conn = sqlite3.connect(database.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        # IMMEDIATE takes the write lock up front so concurrent starters migrate one at a time.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            if applied and max(applied) > LATEST_VERSION:
                raise DatabaseInitializationError(
                    f"Database schema version {max(applied)} is newer than supported version "
                    f"{LATEST_VERSION}"
                )
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _enable_wal(conn)
        return LATEST_VERSION
    finally:
        conn.close()


def database_is_ready(database: Database, timeout: float = 1.0) -> bool:
    """Cheap readiness probe: the file opens read-write and the schema is fully migrated."""
    try:
        # mode=rw never creates a missing file, unlike a plain connect().
        uri = f"{database.path.resolve().as_uri()}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False
    return row is not None and row[0] == LATEST_VERSION


def initialize_database(url: str) -> Database:
    """Open (creating if needed) and migrate the database. Raises on any failure: fail closed."""
    try:
        database = Database.from_url(url)
        database.path.parent.mkdir(parents=True, exist_ok=True)
        migrate(database)
    except DatabaseInitializationError:
        raise
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise DatabaseInitializationError(
            f"Could not initialize database: {type(exc).__name__}: {exc}"
        ) from exc
    return database
