import secrets

import pytest

from gateway.auth.bootstrap import build_api_key_service, parse_credential_entries
from gateway.auth.cli import main as create_key_cli
from gateway.config import Settings
from gateway.errors import AuthenticationError
from gateway.auth.keys import generate_api_key


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"api_key_pepper": None, "api_key_hashes": None}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _pepper() -> str:
    return secrets.token_urlsafe(32)


def _run_cli(settings: Settings, capsys: pytest.CaptureFixture[str], client_id: str = "dev") -> tuple[str, str]:
    """Return (raw_key, credential_entry) parsed from the CLI output."""
    assert create_key_cli(["--client-id", client_id], settings=settings) == 0
    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    raw_key = next(line for line in lines if line.startswith("gw_live_"))
    entry = next(line for line in lines if line.startswith(f"{client_id}:"))
    return raw_key, entry


def test_dev_mode_starts_without_pepper() -> None:
    service = build_api_key_service(_settings())
    with pytest.raises(AuthenticationError):
        service.authenticate(generate_api_key())


def test_hashes_without_pepper_rejected() -> None:
    with pytest.raises(ValueError, match="requires API_KEY_PEPPER"):
        build_api_key_service(_settings(api_key_hashes="dev:" + "0" * 16 + ":" + "0" * 64))


def test_production_requires_pepper() -> None:
    with pytest.raises(ValueError, match="production"):
        build_api_key_service(_settings(app_env="production"))


def test_short_pepper_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        build_api_key_service(_settings(api_key_pepper="short"))


def test_malformed_entry_rejected_without_echoing_it() -> None:
    entry = "dev:not-a-valid-entry-value"
    with pytest.raises(ValueError) as exc_info:
        build_api_key_service(_settings(api_key_pepper=_pepper(), api_key_hashes=entry))
    assert "#1" in str(exc_info.value)
    assert entry not in str(exc_info.value)


def test_parse_entries_skips_blanks_and_whitespace() -> None:
    entry = "dev:" + "a" * 16 + ":" + "b" * 64
    records = parse_credential_entries(f" {entry} , ,")
    assert [(r.client_id, r.key_id) for r in records] == [("dev", "a" * 16)]


def test_cli_key_authenticates_after_bootstrap(capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(api_key_pepper=_pepper())
    raw_key, entry = _run_cli(settings, capsys)

    service = build_api_key_service(_settings(api_key_pepper=settings.api_key_pepper, api_key_hashes=entry))

    assert service.authenticate(raw_key).client_id == "dev"


def test_multiple_entries_loaded(capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(api_key_pepper=_pepper())
    key_a, entry_a = _run_cli(settings, capsys, "client-a")
    key_b, entry_b = _run_cli(settings, capsys, "client-b")

    service = build_api_key_service(
        _settings(api_key_pepper=settings.api_key_pepper, api_key_hashes=f"{entry_a},{entry_b}")
    )

    assert service.authenticate(key_a).client_id == "client-a"
    assert service.authenticate(key_b).client_id == "client-b"


def test_bootstrapped_key_fails_with_different_pepper(capsys: pytest.CaptureFixture[str]) -> None:
    raw_key, entry = _run_cli(_settings(api_key_pepper=_pepper()), capsys)
    service = build_api_key_service(_settings(api_key_pepper=_pepper(), api_key_hashes=entry))

    with pytest.raises(AuthenticationError):
        service.authenticate(raw_key)


def test_cli_prints_raw_key_once_and_entry_has_only_hash(capsys: pytest.CaptureFixture[str]) -> None:
    assert create_key_cli(["--client-id", "dev"], settings=_settings(api_key_pepper=_pepper())) == 0
    output = capsys.readouterr().out

    raw_key = next(tok for tok in output.split() if tok.startswith("gw_live_"))
    entry = next(tok for tok in output.split() if tok.startswith("dev:"))
    assert output.count(raw_key) == 1
    assert raw_key.rsplit("_", 1)[-1] not in entry


def test_cli_requires_pepper(capsys: pytest.CaptureFixture[str]) -> None:
    assert create_key_cli(["--client-id", "dev"], settings=_settings()) == 1
    captured = capsys.readouterr()
    assert "API_KEY_PEPPER" in captured.err
    assert "gw_live_" not in captured.out + captured.err


def test_cli_rejects_invalid_client_id() -> None:
    with pytest.raises(SystemExit) as exc_info:
        create_key_cli(["--client-id", "bad id!"], settings=_settings(api_key_pepper=_pepper()))
    assert exc_info.value.code == 2
