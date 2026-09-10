import logging
import re
from datetime import datetime, timezone
from typing import NoReturn

from gateway.auth.hashing import ApiKeyHasher
from gateway.auth.keys import generate_api_key, parse_key_id
from gateway.auth.models import ApiKeyRecord, AuthenticatedClient, IssuedApiKey
from gateway.auth.store import ApiKeyStore
from gateway.errors import AuthenticationError
from gateway.observability import metrics

logger = logging.getLogger(__name__)

CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


def validate_client_id(client_id: str) -> str:
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError("client_id must be 1-64 characters of letters, digits, '.', '_' or '-'")
    return client_id


class ApiKeyService:
    def __init__(self, store: ApiKeyStore, hasher: ApiKeyHasher) -> None:
        self._store = store
        self._hasher = hasher
        self._dummy_hash = hasher.hash(generate_api_key())

    def create_key(self, client_id: str) -> IssuedApiKey:
        api_key = generate_api_key()
        key_id = parse_key_id(api_key)
        assert key_id is not None
        record = ApiKeyRecord(
            key_id=key_id,
            key_hash=self._hasher.hash(api_key),
            client_id=validate_client_id(client_id),
            created_at=datetime.now(timezone.utc),
        )
        self._store.add(record)
        return IssuedApiKey(api_key=api_key, record=record)

    def revoke_key(self, key_id: str) -> bool:
        return self._store.revoke(key_id, datetime.now(timezone.utc))

    def authenticate(self, api_key: str) -> AuthenticatedClient:
        key_id = parse_key_id(api_key)
        record = self._store.get(key_id) if key_id else None
        # Always perform one HMAC comparison so unknown key ids follow the same path as known ones.
        expected_hash = record.key_hash if record else self._dummy_hash
        hash_matches = self._hasher.verify(api_key, expected_hash)

        if key_id is None:
            reject_authentication("malformed_key", key_id)
        if record is None:
            reject_authentication("unknown_key", key_id)
        if not hash_matches:
            reject_authentication("hash_mismatch", key_id)
        if record.revoked:
            reject_authentication("revoked_key", key_id)
        return AuthenticatedClient(client_id=record.client_id, key_id=record.key_id)


def reject_authentication(reason: str, key_id: str | None = None) -> NoReturn:
    """Log and count a failure, then raise the generic error. `reason` is a fixed slug."""
    # Only the non-secret key id is logged; the client always gets the same generic error.
    logger.info(
        "API key authentication failed: %s (key_id=%s)", reason, key_id or "-",
        extra={"reason": reason, "key_id": key_id},
    )
    metrics.AUTH_FAILURES.labels(reason).inc()
    raise AuthenticationError()
