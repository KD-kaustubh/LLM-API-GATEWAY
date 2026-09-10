import logging
import secrets
import sqlite3
from pathlib import Path

import pytest

from gateway.auth.bootstrap import build_api_key_service
from gateway.auth.cli import main as create_key_cli
from gateway.auth.cli import revoke_main as revoke_key_cli
from gateway.auth.keys import generate_api_key
from gateway.auth.store import InMemoryApiKeyStore
from gateway.config import Settings
from gateway.errors import AuthenticationError
from gateway.persistence.migrations import initialize_database
from gateway.persistence.repositories import SQLiteApiKeyStore
from tests.conftest import secret_of


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key_pepper": None,
        "database_url": f"sqlite:///{(tmp_path / 'cli.db').as_posix()}",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _pepper() -> str:
    return secrets.token_urlsafe(32)


def _create(settings: Settings, capsys: pytest.CaptureFixture[str], client_id: str = "dev") -> tuple[str, str]:
    """Run the CLI; return (raw_key, full stdout)."""
    assert create_key_cli(["--client-id", client_id], settings=settings) == 0
    out = capsys.readouterr().out
    raw_key = next(tok for tok in out.split() if tok.startswith("gw_live_"))
    return raw_key, out


def _service_for(settings: Settings):
    store = SQLiteApiKeyStore(initialize_database(settings.database_url))
    return build_api_key_service(settings, store)


# --- build_api_key_service ---------------------------------------------------


def test_dev_mode_without_pepper_starts_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    service = build_api_key_service(_settings(tmp_path), InMemoryApiKeyStore())

    assert "API_KEY_PEPPER is not set" in caplog.text
    with pytest.raises(AuthenticationError):
        service.authenticate(generate_api_key())


def test_production_requires_pepper(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="production"):
        build_api_key_service(_settings(tmp_path, app_env="production"), InMemoryApiKeyStore())


def test_short_pepper_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        build_api_key_service(_settings(tmp_path, api_key_pepper="short"), InMemoryApiKeyStore())


# --- gateway-create-key ------------------------------------------------------


def test_cli_persists_key_that_authenticates_after_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path, api_key_pepper=_pepper())
    raw_key, _ = _create(settings, capsys)

    client = _service_for(settings).authenticate(raw_key)  # fresh store and service: a "restart"

    assert client.client_id == "dev"


def test_cli_prints_raw_key_once_and_stores_only_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path, api_key_pepper=_pepper())
    raw_key, out = _create(settings, capsys)

    assert out.count(raw_key) == 1
    db_bytes = b"".join(p.read_bytes() for p in tmp_path.glob("cli.db*"))
    assert raw_key.encode() not in db_bytes
    assert secret_of(raw_key).encode() not in db_bytes
    assert settings.api_key_pepper.get_secret_value().encode() not in db_bytes


def test_cli_supports_multiple_keys_per_client(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path, api_key_pepper=_pepper())
    first, _ = _create(settings, capsys, "team-a")
    second, _ = _create(settings, capsys, "team-a")

    service = _service_for(settings)

    assert service.authenticate(first).client_id == "team-a"
    assert service.authenticate(second).client_id == "team-a"
    assert service.authenticate(first).key_id != service.authenticate(second).key_id


def test_key_fails_with_different_pepper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw_key, _ = _create(_settings(tmp_path, api_key_pepper=_pepper()), capsys)

    with pytest.raises(AuthenticationError):
        _service_for(_settings(tmp_path, api_key_pepper=_pepper())).authenticate(raw_key)


def test_cli_requires_pepper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert create_key_cli(["--client-id", "dev"], settings=_settings(tmp_path)) == 1
    captured = capsys.readouterr()
    assert "API_KEY_PEPPER" in captured.err
    assert "gw_live_" not in captured.out + captured.err
    assert not (tmp_path / "cli.db").exists()


def test_cli_rejects_invalid_client_id(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        create_key_cli(["--client-id", "bad id!"], settings=_settings(tmp_path, api_key_pepper=_pepper()))
    assert exc_info.value.code == 2


def test_cli_reports_unusable_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "cli.db").write_bytes(b"this is not a sqlite database" * 100)
    settings = _settings(tmp_path, api_key_pepper=_pepper())

    assert create_key_cli(["--client-id", "dev"], settings=settings) == 1
    captured = capsys.readouterr()
    assert "Could not initialize database" in captured.err
    assert "gw_live_" not in captured.out


# --- gateway-revoke-key ------------------------------------------------------


def test_revoke_cli_revokes_persisted_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path, api_key_pepper=_pepper())
    raw_key, out = _create(settings, capsys)
    key_id = raw_key[len("gw_live_"):len("gw_live_") + 16]
    assert key_id in out

    assert revoke_key_cli(["--key-id", key_id], settings=settings) == 0
    assert f"Revoked API key {key_id}" in capsys.readouterr().out

    with pytest.raises(AuthenticationError):
        _service_for(settings).authenticate(raw_key)


def test_revoke_cli_unknown_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revoke_key_cli(["--key-id", "0" * 16], settings=_settings(tmp_path)) == 1
    assert "No API key" in capsys.readouterr().err


@pytest.mark.parametrize("key_id", ["short", "G" * 16, "0" * 17, "' OR 1=1 --"])
def test_revoke_cli_rejects_malformed_key_id(tmp_path: Path, key_id: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        revoke_key_cli(["--key-id", key_id], settings=_settings(tmp_path))
    assert exc_info.value.code == 2


def test_revoke_does_not_need_pepper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pepper = _pepper()
    raw_key, _ = _create(_settings(tmp_path, api_key_pepper=pepper), capsys)
    key_id = raw_key[len("gw_live_"):len("gw_live_") + 16]

    assert revoke_key_cli(["--key-id", key_id], settings=_settings(tmp_path)) == 0

    with sqlite3.connect(tmp_path / "cli.db") as conn:
        revoked_at = conn.execute("SELECT revoked_at FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()[0]
    assert revoked_at is not None
