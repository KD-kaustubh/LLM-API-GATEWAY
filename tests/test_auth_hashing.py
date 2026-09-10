import hmac
import secrets

import pytest

from gateway.auth import hashing
from gateway.auth.hashing import ApiKeyHasher
from gateway.auth.keys import generate_api_key


@pytest.fixture
def hasher() -> ApiKeyHasher:
    return ApiKeyHasher(secrets.token_bytes(32))


def test_same_key_verifies(hasher: ApiKeyHasher) -> None:
    api_key = generate_api_key()
    assert hasher.verify(api_key, hasher.hash(api_key))


def test_wrong_key_fails(hasher: ApiKeyHasher) -> None:
    assert not hasher.verify(generate_api_key(), hasher.hash(generate_api_key()))


def test_hash_is_deterministic_hex_sha256(hasher: ApiKeyHasher) -> None:
    api_key = generate_api_key()
    digest = hasher.hash(api_key)
    assert digest == hasher.hash(api_key)
    assert len(digest) == 64
    int(digest, 16)


def test_hash_does_not_contain_key(hasher: ApiKeyHasher) -> None:
    api_key = generate_api_key()
    secret_part = api_key.rsplit("_", 1)[-1]
    assert secret_part not in hasher.hash(api_key)


def test_hash_depends_on_pepper() -> None:
    api_key = generate_api_key()
    first = ApiKeyHasher(secrets.token_bytes(32))
    second = ApiKeyHasher(secrets.token_bytes(32))
    assert first.hash(api_key) != second.hash(api_key)
    assert not second.verify(api_key, first.hash(api_key))


def test_short_pepper_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ApiKeyHasher(b"too-short")


def test_non_hex_expected_hash_fails_safely(hasher: ApiKeyHasher) -> None:
    assert not hasher.verify(generate_api_key(), "not-a-hash-é")


def test_verify_uses_timing_safe_comparison(
    hasher: ApiKeyHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hashing.hmac, "compare_digest", spy)
    api_key = generate_api_key()

    assert hasher.verify(api_key, hasher.hash(api_key))
    assert len(calls) == 1
