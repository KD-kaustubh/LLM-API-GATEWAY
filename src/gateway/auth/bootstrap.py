import re
import secrets
from datetime import datetime, timezone

from gateway.auth.hashing import MIN_PEPPER_BYTES, ApiKeyHasher
from gateway.auth.models import ApiKeyRecord
from gateway.auth.service import CLIENT_ID_PATTERN, ApiKeyService
from gateway.auth.store import InMemoryApiKeyStore
from gateway.config import Settings

_ENTRY_PATTERN = re.compile(
    rf"(?P<client_id>{CLIENT_ID_PATTERN.pattern}):(?P<key_id>[0-9a-f]{{16}}):(?P<key_hash>[0-9a-f]{{64}})"
)


def format_credential_entry(record: ApiKeyRecord) -> str:
    return f"{record.client_id}:{record.key_id}:{record.key_hash}"


def parse_credential_entries(value: str) -> list[ApiKeyRecord]:
    """Parse comma-separated `client_id:key_id:key_hash` entries from API_KEY_HASHES."""
    loaded_at = datetime.now(timezone.utc)
    records = []
    for position, entry in enumerate(filter(None, (e.strip() for e in value.split(","))), start=1):
        match = _ENTRY_PATTERN.fullmatch(entry)
        if match is None:
            raise ValueError(f"API_KEY_HASHES entry #{position} is malformed")
        records.append(ApiKeyRecord(created_at=loaded_at, **match.groupdict()))
    return records


def hasher_from_settings(settings: Settings) -> ApiKeyHasher | None:
    if settings.api_key_pepper is None:
        return None
    return ApiKeyHasher(settings.api_key_pepper.get_secret_value().encode())


def build_api_key_service(settings: Settings) -> ApiKeyService:
    hasher = hasher_from_settings(settings)
    entries = settings.api_key_hashes.get_secret_value() if settings.api_key_hashes else ""

    if hasher is None:
        if entries:
            raise ValueError("API_KEY_HASHES requires API_KEY_PEPPER to be set")
        if settings.app_env == "production":
            raise ValueError("API_KEY_PEPPER must be set in production")
        # No configured credentials: an ephemeral pepper is safe because nothing outlives the process.
        hasher = ApiKeyHasher(secrets.token_bytes(MIN_PEPPER_BYTES))

    store = InMemoryApiKeyStore()
    for record in parse_credential_entries(entries):
        store.add(record)
    return ApiKeyService(store, hasher)
