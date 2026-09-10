"""Deployment support: API_KEY_SEEDS, `gateway-create-key --seed`, and platform hostnames."""

import logging
import secrets
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from gateway.auth.bootstrap import format_seed_entry, parse_seed_entries, seed_api_keys
from gateway.auth.cli import main as create_key_cli
from gateway.auth.hashing import ApiKeyHasher
from gateway.auth.service import ApiKeyService
from gateway.auth.store import InMemoryApiKeyStore
from gateway.config import Settings
from gateway.errors import AuthenticationError
from gateway.persistence.database import Database
from gateway.persistence.repositories import SQLiteApiKeyStore
from tests.conftest import Gateway, secret_of

URL = "/v1/chat/completions"
PAYLOAD: dict[str, Any] = {"model": "mock", "messages": [{"role": "user", "content": "Hi"}]}


def _pepper() -> str:
    return secrets.token_urlsafe(32)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{"api_key_pepper": None, "api_key_seeds": None, **overrides})


def _seed(pepper: str, client_id: str = "demo") -> tuple[str, str]:
    """Return (raw_key, seed_entry) created exactly as `gateway-create-key --seed` does."""
    issued = ApiKeyService(InMemoryApiKeyStore(), ApiKeyHasher(pepper.encode())).create_key(client_id)
    return issued.api_key, format_seed_entry(issued.record)


# --- seeding -----------------------------------------------------------------------


def test_no_seeds_is_a_no_op(database: Database) -> None:
    assert seed_api_keys(_settings(api_key_pepper=_pepper()), SQLiteApiKeyStore(database)) == 0


def test_seeded_key_authenticates(database: Database) -> None:
    pepper = _pepper()
    raw_key, entry = _seed(pepper)
    store = SQLiteApiKeyStore(database)

    assert seed_api_keys(_settings(api_key_pepper=pepper, api_key_seeds=entry), store) == 1

    service = ApiKeyService(store, ApiKeyHasher(pepper.encode()))
    assert service.authenticate(raw_key).client_id == "demo"


def test_seeding_is_idempotent_and_multi_entry(database: Database) -> None:
    pepper = _pepper()
    (key_a, entry_a), (key_b, entry_b) = _seed(pepper, "team-a"), _seed(pepper, "team-b")
    settings = _settings(api_key_pepper=pepper, api_key_seeds=f" {entry_a} , {entry_b} ,")
    store = SQLiteApiKeyStore(database)

    assert seed_api_keys(settings, store) == 2
    assert seed_api_keys(settings, store) == 0

    service = ApiKeyService(store, ApiKeyHasher(pepper.encode()))
    assert service.authenticate(key_a).client_id == "team-a"
    assert service.authenticate(key_b).client_id == "team-b"


def test_seeding_never_unrevokes_a_stored_key(database: Database) -> None:
    pepper = _pepper()
    raw_key, entry = _seed(pepper)
    store = SQLiteApiKeyStore(database)
    settings = _settings(api_key_pepper=pepper, api_key_seeds=entry)
    seed_api_keys(settings, store)
    service = ApiKeyService(store, ApiKeyHasher(pepper.encode()))
    service.revoke_key(raw_key[8:24])

    seed_api_keys(settings, store)

    with pytest.raises(AuthenticationError):
        service.authenticate(raw_key)


def test_conflicting_seed_keeps_stored_key(database: Database, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    pepper = _pepper()
    raw_key, entry = _seed(pepper)
    store = SQLiteApiKeyStore(database)
    seed_api_keys(_settings(api_key_pepper=pepper, api_key_seeds=entry), store)
    client_id, key_id, _ = entry.split(":")

    seed_api_keys(_settings(api_key_pepper=pepper, api_key_seeds=f"{client_id}:{key_id}:{'0' * 64}"), store)

    assert ApiKeyService(store, ApiKeyHasher(pepper.encode())).authenticate(raw_key).key_id == key_id
    assert "conflicts" in caplog.text


def test_seeds_require_pepper(database: Database) -> None:
    _, entry = _seed(_pepper())
    with pytest.raises(ValueError, match="API_KEY_SEEDS requires API_KEY_PEPPER"):
        seed_api_keys(_settings(api_key_seeds=entry), SQLiteApiKeyStore(database))


def test_seed_with_wrong_pepper_does_not_authenticate(database: Database) -> None:
    raw_key, entry = _seed(_pepper())
    other_pepper = _pepper()
    store = SQLiteApiKeyStore(database)
    seed_api_keys(_settings(api_key_pepper=other_pepper, api_key_seeds=entry), store)

    with pytest.raises(AuthenticationError):
        ApiKeyService(store, ApiKeyHasher(other_pepper.encode())).authenticate(raw_key)


@pytest.mark.parametrize(
    "value",
    ["demo", "demo:short:hash", "bad id!:0123456789abcdef:" + "a" * 64, "demo:0123456789abcdef:" + "g" * 64],
)
def test_malformed_seed_rejected_without_echo(value: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_seed_entries(value)
    assert "#1" in str(exc_info.value)
    assert value not in str(exc_info.value)


def test_seeds_setting_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = "demo:0123456789abcdef:" + "a" * 64
    monkeypatch.setenv("API_KEY_SEEDS", entry)
    settings = Settings(_env_file=None)
    assert isinstance(settings.api_key_seeds, SecretStr)
    assert entry not in repr(settings)


# --- CLI --seed ----------------------------------------------------------------------


def test_cli_seed_prints_entry_and_key_without_touching_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pepper = _pepper()
    db_path = tmp_path / "never.db"
    settings = _settings(api_key_pepper=pepper, database_url=f"sqlite:///{db_path.as_posix()}")

    assert create_key_cli(["--client-id", "demo", "--seed"], settings=settings) == 0

    out = capsys.readouterr().out
    raw_key = next(tok for tok in out.split() if tok.startswith("gw_live_"))
    entry = next(tok for tok in out.split() if tok.startswith("demo:"))
    assert out.count(raw_key) == 1
    assert secret_of(raw_key) not in entry
    assert not db_path.exists()
    [record] = parse_seed_entries(entry)
    assert ApiKeyHasher(pepper.encode()).verify(raw_key, record.key_hash)


def test_cli_seed_requires_pepper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert create_key_cli(["--client-id", "demo", "--seed"], settings=_settings()) == 1
    assert "API_KEY_PEPPER" in capsys.readouterr().err


# --- end to end: startup seeding and Render hostname ---------------------------------


def test_startup_seeds_keys_for_shell_less_deployments(make_gateway: Any) -> None:
    gateway: Gateway = make_gateway("seeded.db")
    raw_key, entry = _seed(gateway.pepper)
    gateway._env["API_KEY_SEEDS"] = entry

    client = gateway.start()

    assert client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {raw_key}"}).status_code == 200
    assert gateway.rows("SELECT client_id FROM api_keys") == [("demo",)]
    assert raw_key.encode() not in gateway.file_bytes()


def test_malformed_seeds_fail_startup(make_gateway: Any) -> None:
    gateway: Gateway = make_gateway("badseed.db", API_KEY_SEEDS="not-an-entry")
    with pytest.raises(ValueError, match="API_KEY_SEEDS entry #1 is malformed"):
        gateway.start()


def test_render_hostname_is_trusted_exactly(make_gateway: Any) -> None:
    gateway: Gateway = make_gateway("render.db", RENDER_EXTERNAL_HOSTNAME="llm-api-gateway.onrender.com")
    client = gateway.start()

    assert client.get("/health", headers={"Host": "llm-api-gateway.onrender.com"}).status_code == 200
    assert client.get("/health", headers={"Host": "other-service.onrender.com"}).status_code == 400
    assert client.get("/health", headers={"Host": "evil.example.net"}).status_code == 400


def test_render_hostname_appended_to_configured_hosts() -> None:
    settings = _settings(trusted_hosts="api.example.com", render_external_hostname="gw.onrender.com")
    assert settings.trusted_host_list == ["api.example.com", "gw.onrender.com"]
    assert _settings(trusted_hosts="gw.onrender.com", render_external_hostname="gw.onrender.com").trusted_host_list == ["gw.onrender.com"]
