import re
import secrets

import pytest

from gateway.auth import keys
from gateway.auth.keys import API_KEY_PREFIX, generate_api_key, parse_key_id

KEY_SHAPE = re.compile(r"gw_live_[0-9a-f]{16}_[A-Za-z0-9_-]{43}")


def test_generated_key_has_gateway_prefix_and_shape() -> None:
    api_key = generate_api_key()
    assert api_key.startswith(API_KEY_PREFIX)
    assert KEY_SHAPE.fullmatch(api_key)


def test_generated_key_meets_minimum_length() -> None:
    assert len(generate_api_key()) >= len(API_KEY_PREFIX) + 16 + 1 + 43


def test_generated_keys_are_unique() -> None:
    generated = {generate_api_key() for _ in range(1000)}
    assert len(generated) == 1000


def test_key_material_comes_from_secrets_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    real_hex, real_urlsafe = secrets.token_hex, secrets.token_urlsafe

    def spy_hex(nbytes: int) -> str:
        calls.append(("token_hex", nbytes))
        return real_hex(nbytes)

    def spy_urlsafe(nbytes: int) -> str:
        calls.append(("token_urlsafe", nbytes))
        return real_urlsafe(nbytes)

    monkeypatch.setattr(keys.secrets, "token_hex", spy_hex)
    monkeypatch.setattr(keys.secrets, "token_urlsafe", spy_urlsafe)

    generate_api_key()

    assert ("token_urlsafe", 32) in calls  # 256-bit secret
    assert ("token_hex", 8) in calls


def test_parse_key_id_extracts_id() -> None:
    api_key = generate_api_key()
    assert parse_key_id(api_key) == api_key[len(API_KEY_PREFIX) : len(API_KEY_PREFIX) + 16]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "gw_live_invalid_test_key",
        "sk-not-a-gateway-key",
        "gw_test_0123456789abcdef_" + "a" * 43,
        "gw_live_0123456789ABCDEF_" + "a" * 43,
        "gw_live_0123456789abcdef_" + "a" * 42,
        "gw_live_0123456789abcdef_" + "a" * 44,
        " gw_live_0123456789abcdef_" + "a" * 43,
        "gw_live_0123456789abcdef_" + "a" * 42 + "!",
    ],
)
def test_parse_key_id_rejects_malformed(value: str) -> None:
    assert parse_key_id(value) is None
