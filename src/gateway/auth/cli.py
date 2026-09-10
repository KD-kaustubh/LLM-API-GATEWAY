"""Development key management: create or revoke gateway API keys in the configured database.

Usage:
  gateway-create-key --client-id <client-id>
  gateway-revoke-key --key-id <key-id>
"""

import argparse
import re
import sys
from datetime import datetime, timezone

from gateway.auth.bootstrap import hasher_from_settings
from gateway.auth.service import ApiKeyService, validate_client_id
from gateway.config import Settings, get_settings
from gateway.persistence.database import DatabaseInitializationError
from gateway.persistence.migrations import initialize_database
from gateway.persistence.repositories import SQLiteApiKeyStore

_PEPPER_HINT = (
    "API_KEY_PEPPER is not set. Add a random value of at least 32 characters to your local .env,\n"
    "for example the output of:\n"
    '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
)
_KEY_ID_PATTERN = re.compile(r"[0-9a-f]{16}")


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    """Create a key, store only its hash, and print the raw key exactly once."""
    parser = argparse.ArgumentParser(
        prog="gateway-create-key", description="Create a gateway API key and store its hash."
    )
    parser.add_argument("--client-id", required=True, type=_client_id)
    args = parser.parse_args(argv)
    settings = settings or get_settings()

    hasher = hasher_from_settings(settings)
    if hasher is None:
        print(_PEPPER_HINT, file=sys.stderr)
        return 1
    store = _open_store(settings)
    if store is None:
        return 1

    issued = ApiKeyService(store, hasher).create_key(args.client_id)
    print(f"Created API key for client '{args.client_id}' (key id {issued.record.key_id}).")
    print("The key is shown once and cannot be recovered; only its hash is stored:\n")
    print(f"  {issued.api_key}")
    return 0


def revoke_main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gateway-revoke-key", description="Revoke a gateway API key by its key id."
    )
    parser.add_argument("--key-id", required=True, type=_key_id)
    args = parser.parse_args(argv)

    store = _open_store(settings or get_settings())
    if store is None:
        return 1
    if not store.revoke(args.key_id, datetime.now(timezone.utc)):
        print(f"No API key with key id {args.key_id}.", file=sys.stderr)
        return 1
    print(f"Revoked API key {args.key_id}.")
    return 0


def _open_store(settings: Settings) -> SQLiteApiKeyStore | None:
    try:
        return SQLiteApiKeyStore(initialize_database(settings.database_url))
    except DatabaseInitializationError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _client_id(value: str) -> str:
    try:
        return validate_client_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _key_id(value: str) -> str:
    if not _KEY_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("key id must be 16 lowercase hex characters")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
