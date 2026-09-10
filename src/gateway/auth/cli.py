"""Development helper: create a gateway API key and print it once.

Usage: python -m gateway.auth.cli --client-id <client-id>
"""

import argparse
import sys

from gateway.auth.bootstrap import format_credential_entry, hasher_from_settings
from gateway.auth.service import ApiKeyService, validate_client_id
from gateway.auth.store import InMemoryApiKeyStore
from gateway.config import Settings, get_settings

_PEPPER_HINT = (
    "API_KEY_PEPPER is not set. Add a random value of at least 32 characters to your local .env,\n"
    "for example the output of:\n"
    '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
)


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gateway-create-key", description="Create a gateway API key for local development."
    )
    parser.add_argument("--client-id", required=True, type=_client_id)
    args = parser.parse_args(argv)

    hasher = hasher_from_settings(settings or get_settings())
    if hasher is None:
        print(_PEPPER_HINT, file=sys.stderr)
        return 1

    issued = ApiKeyService(InMemoryApiKeyStore(), hasher).create_key(args.client_id)
    print(f"API key for client '{args.client_id}' (shown once; it cannot be recovered):\n")
    print(f"  {issued.api_key}\n")
    print("Append this entry to API_KEY_HASHES in your local .env (it contains only a hash):\n")
    print(f"  {format_credential_entry(issued.record)}")
    return 0


def _client_id(value: str) -> str:
    try:
        return validate_client_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
