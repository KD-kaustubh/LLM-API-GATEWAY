import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SQLITE_URL_PREFIX = "sqlite:///"
BUSY_TIMEOUT_SECONDS = 5.0


class DatabaseInitializationError(RuntimeError):
    """Raised at startup when the database cannot be opened or migrated safely."""


def sqlite_path_from_url(url: str) -> Path:
    """Parse `sqlite:///relative/path.db` or `sqlite:////absolute/path.db` into a file path."""
    if not url.startswith(SQLITE_URL_PREFIX):
        raise ValueError("DATABASE_URL must use the form sqlite:///<path to .db file>")
    raw_path = url[len(SQLITE_URL_PREFIX):]
    if not raw_path or raw_path.startswith(":memory:"):
        raise ValueError("DATABASE_URL must point to a database file, not an in-memory database")
    return Path(raw_path)


class Database:
    """Opens a short-lived connection per operation; never shares a connection across threads."""

    def __init__(self, path: Path, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> None:
        self.path = path
        self._busy_timeout = busy_timeout

    @classmethod
    def from_url(cls, url: str) -> "Database":
        return cls(sqlite_path_from_url(url))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction that commits on success and rolls back on error."""
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout)
        try:
            with conn:
                yield conn
        finally:
            conn.close()
