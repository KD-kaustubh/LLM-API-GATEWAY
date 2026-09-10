from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ApiKeyRecord:
    """Stored credential. Holds only the key hash, never the raw key."""

    key_id: str
    key_hash: str
    client_id: str
    created_at: datetime
    revoked_at: datetime | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class IssuedApiKey:
    """Result of key creation; the only place the raw key exists."""

    api_key: str = field(repr=False)
    record: ApiKeyRecord


@dataclass(frozen=True)
class AuthenticatedClient:
    client_id: str
    key_id: str
