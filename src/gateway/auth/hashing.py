import hashlib
import hmac

MIN_PEPPER_BYTES = 32


class ApiKeyHasher:
    """HMAC-SHA256 keyed with a server-side pepper.

    Gateway keys carry 256 bits of randomness, so a fast keyed hash is sufficient: brute force
    is infeasible, and without the pepper a leaked hash cannot be checked offline. Slow password
    hashes (bcrypt/argon2) protect low-entropy passwords and would only add per-request latency.
    """

    def __init__(self, pepper: bytes) -> None:
        if len(pepper) < MIN_PEPPER_BYTES:
            raise ValueError(f"API key pepper must be at least {MIN_PEPPER_BYTES} bytes")
        self._pepper = pepper

    def hash(self, api_key: str) -> str:
        return hmac.new(self._pepper, api_key.encode(), hashlib.sha256).hexdigest()

    def verify(self, api_key: str, expected_hash: str) -> bool:
        # compare_digest runs in time independent of where the inputs first differ.
        return hmac.compare_digest(self.hash(api_key).encode(), expected_hash.encode())
