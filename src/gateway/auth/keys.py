import re
import secrets

API_KEY_PREFIX = "gw_live_"

_KEY_ID_BYTES = 8
_SECRET_BYTES = 32
_API_KEY_PATTERN = re.compile(r"gw_live_([0-9a-f]{16})_([A-Za-z0-9_-]{43})")


def generate_api_key() -> str:
    """Return a new key: gw_live_<16 hex key id>_<43 char url-safe secret, 256 bits>."""
    key_id = secrets.token_hex(_KEY_ID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return f"{API_KEY_PREFIX}{key_id}_{secret}"


def parse_key_id(api_key: str) -> str | None:
    """Return the non-secret key id embedded in a well-formed key, else None."""
    match = _API_KEY_PATTERN.fullmatch(api_key)
    return match.group(1) if match else None
