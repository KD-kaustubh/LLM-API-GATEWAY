import logging
import re
import secrets
from datetime import datetime, timezone

from gateway.auth.hashing import MIN_PEPPER_BYTES, ApiKeyHasher
from gateway.auth.models import ApiKeyRecord
from gateway.auth.service import CLIENT_ID_PATTERN, ApiKeyService
from gateway.auth.store import ApiKeyStore
from gateway.config import Settings

logger = logging.getLogger(__name__)

_SEED_PATTERN = re.compile(
    rf"(?P<client_id>{CLIENT_ID_PATTERN.pattern}):(?P<key_id>[0-9a-f]{{16}}):(?P<key_hash>[0-9a-f]{{64}})"
)


def format_seed_entry(record: ApiKeyRecord) -> str:
    return f"{record.client_id}:{record.key_id}:{record.key_hash}"


def parse_seed_entries(value: str) -> list[ApiKeyRecord]:
    """Parse comma-separated `client_id:key_id:key_hash` entries. Messages never echo entries."""
    now = datetime.now(timezone.utc)
    records = []
    for position, entry in enumerate(filter(None, (e.strip() for e in value.split(","))), start=1):
        match = _SEED_PATTERN.fullmatch(entry)
        if match is None:
            raise ValueError(f"API_KEY_SEEDS entry #{position} is malformed")
        records.append(ApiKeyRecord(created_at=now, **match.groupdict()))
    return records


def seed_api_keys(settings: Settings, store: ApiKeyStore) -> int:
    """Insert API_KEY_SEEDS entries whose key id is not stored yet; return how many were added.

    Existing rows are never modified, so a key revoked in the database stays revoked. This is
    how keys reach deployments that have no shell and no persistent disk.
    """
    if settings.api_key_seeds is None:
        return 0
    if settings.api_key_pepper is None:
        raise ValueError("API_KEY_SEEDS requires API_KEY_PEPPER (the pepper used to create them)")
    added = 0
    for record in parse_seed_entries(settings.api_key_seeds.get_secret_value()):
        existing = store.get(record.key_id)
        if existing is None:
            store.add(record)
            added += 1
        elif existing.key_hash != record.key_hash or existing.client_id != record.client_id:
            logger.warning(
                "API_KEY_SEEDS entry conflicts with a stored key; the stored key is kept",
                extra={"key_id": record.key_id},
            )
    if added:
        logger.info("Seeded %d API key(s) from API_KEY_SEEDS", added)
    return added


def hasher_from_settings(settings: Settings) -> ApiKeyHasher | None:
    if settings.api_key_pepper is None:
        return None
    return ApiKeyHasher(settings.api_key_pepper.get_secret_value().encode())


def build_api_key_service(settings: Settings, store: ApiKeyStore) -> ApiKeyService:
    hasher = hasher_from_settings(settings)
    if hasher is None:
        if settings.app_env == "production":
            raise ValueError("API_KEY_PEPPER must be set in production")
        logger.warning(
            "API_KEY_PEPPER is not set: using a random per-process pepper, so stored API keys "
            "cannot be verified and every authenticated request will be rejected"
        )
        hasher = ApiKeyHasher(secrets.token_bytes(MIN_PEPPER_BYTES))
    return ApiKeyService(store, hasher)
