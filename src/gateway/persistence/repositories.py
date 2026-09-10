import sqlite3
from datetime import datetime

from gateway.auth.models import ApiKeyRecord
from gateway.persistence.database import Database
from gateway.services.usage import UsageRecord

# All SQL below uses `?` placeholders; no value is ever interpolated into a statement.


class SQLiteApiKeyStore:
    """ApiKeyStore backed by SQLite. Stores only the key hash, never the raw key."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def add(self, record: ApiKeyRecord) -> None:
        try:
            with self._db.connection() as conn:
                conn.execute(
                    "INSERT INTO api_keys (key_id, key_hash, client_id, created_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record.key_id,
                        record.key_hash,
                        record.client_id,
                        record.created_at.isoformat(),
                        record.revoked_at.isoformat() if record.revoked_at else None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Duplicate key id: {record.key_id}") from exc

    def get(self, key_id: str) -> ApiKeyRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT key_id, key_hash, client_id, created_at, revoked_at "
                "FROM api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        if row is None:
            return None
        return ApiKeyRecord(
            key_id=row[0],
            key_hash=row[1],
            client_id=row[2],
            created_at=datetime.fromisoformat(row[3]),
            revoked_at=datetime.fromisoformat(row[4]) if row[4] else None,
        )

    def revoke(self, key_id: str, revoked_at: datetime) -> bool:
        with self._db.connection() as conn:
            # Keeps the first revocation timestamp if the key was already revoked.
            conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
                (revoked_at.isoformat(), key_id),
            )
            exists = conn.execute("SELECT 1 FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        return exists is not None


class SQLiteUsageRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, record: UsageRecord) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO usage_records (request_id, client_id, key_id, model, provider, "
                "created_at, input_tokens, output_tokens, total_tokens, latency_ms, cache_hit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.client_id,
                    record.key_id,
                    record.model,
                    record.provider,
                    record.created_at.isoformat(),
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                    record.latency_ms,
                    int(record.cache_hit),
                ),
            )

    def list_for_client(self, client_id: str, limit: int = 100) -> list[UsageRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT request_id, client_id, key_id, model, provider, created_at, input_tokens, "
                "output_tokens, total_tokens, latency_ms, cache_hit FROM usage_records "
                "WHERE client_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (client_id, limit),
            ).fetchall()
        return [
            UsageRecord(
                request_id=r[0], client_id=r[1], key_id=r[2], model=r[3], provider=r[4],
                created_at=datetime.fromisoformat(r[5]), input_tokens=r[6], output_tokens=r[7],
                total_tokens=r[8], latency_ms=r[9], cache_hit=bool(r[10]),
            )
            for r in rows
        ]


class SQLiteCacheStore:
    """CacheStore backed by SQLite; expired rows are purged and the table is capped on each write."""

    def __init__(self, database: Database, max_entries: int) -> None:
        self._db = database
        self._max_entries = max_entries

    def get(self, key: str, now: float) -> str | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT response_payload FROM cache_entries WHERE cache_key = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
        return row[0] if row else None

    def put(self, key: str, payload: str, model: str, now: float, expires_at: float) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache_entries "
                "(cache_key, response_payload, model, created_at, expires_at, size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, payload, model, now, expires_at, len(payload.encode("utf-8"))),
            )
            conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (now,))
            # Evict entries closest to expiry until the table is back within the limit.
            conn.execute(
                "DELETE FROM cache_entries WHERE cache_key IN ("
                "SELECT cache_key FROM cache_entries ORDER BY expires_at ASC "
                "LIMIT max(0, (SELECT COUNT(*) FROM cache_entries) - ?))",
                (self._max_entries,),
            )

    def purge_expired(self, now: float) -> int:
        with self._db.connection() as conn:
            return conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (now,)).rowcount

    def __len__(self) -> int:
        with self._db.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
