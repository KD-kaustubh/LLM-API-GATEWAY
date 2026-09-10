import threading
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from gateway.auth.models import ApiKeyRecord


class ApiKeyStore(Protocol):
    def add(self, record: ApiKeyRecord) -> None: ...

    def get(self, key_id: str) -> ApiKeyRecord | None: ...

    def revoke(self, key_id: str, revoked_at: datetime) -> bool: ...


class InMemoryApiKeyStore:
    """Process-local store for development and tests. Not persistent."""

    def __init__(self) -> None:
        self._records: dict[str, ApiKeyRecord] = {}
        self._lock = threading.Lock()

    def add(self, record: ApiKeyRecord) -> None:
        with self._lock:
            if record.key_id in self._records:
                raise ValueError(f"Duplicate key id: {record.key_id}")
            self._records[record.key_id] = record

    def get(self, key_id: str) -> ApiKeyRecord | None:
        return self._records.get(key_id)

    def revoke(self, key_id: str, revoked_at: datetime) -> bool:
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return False
            if not record.revoked:
                self._records[key_id] = replace(record, revoked_at=revoked_at)
            return True
