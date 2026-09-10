import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.config import Settings

PROVIDER_ENV_VARS = (
    "GROQ_API_KEY",
    "GROQ_MODEL_NAME",
    "GOOGLE_API_KEY",
    "GEMINI_MODEL_NAME",
    "API_KEY_PEPPER",
    "DATABASE_URL",
    "CACHE_ENABLED",
    "CACHE_TTL_SECONDS",
    "CACHE_MAX_ENTRIES",
    "PROVIDER_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_DELAY",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
)


@pytest.mark.usefixtures("clean_env")
def test_reliability_and_rate_limit_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.provider_timeout_seconds == 30.0
    assert settings.max_retries == 2
    assert settings.retry_base_delay == 0.5
    assert settings.retry_max_delay == 4.0
    assert settings.rate_limit_requests == 60
    assert settings.rate_limit_window_seconds == 60.0


@pytest.mark.usefixtures("clean_env")
def test_reliability_and_rate_limit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "PROVIDER_TIMEOUT_SECONDS": "12.5",
        "MAX_RETRIES": "4",
        "RETRY_BASE_DELAY": "0.25",
        "RETRY_MAX_DELAY": "2",
        "RATE_LIMIT_REQUESTS": "500",
        "RATE_LIMIT_WINDOW_SECONDS": "30",
    }.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.provider_timeout_seconds == 12.5
    assert settings.max_retries == 4
    assert settings.retry_base_delay == 0.25
    assert settings.retry_max_delay == 2.0
    assert settings.rate_limit_requests == 500
    assert settings.rate_limit_window_seconds == 30.0


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_RETRIES", "-1"),
        ("MAX_RETRIES", "6"),
        ("MAX_RETRIES", "two"),
        ("RETRY_BASE_DELAY", "-0.1"),
        ("RETRY_MAX_DELAY", "61"),
        ("RETRY_MAX_DELAY", "0.1"),  # below the default base delay of 0.5
        ("PROVIDER_TIMEOUT_SECONDS", "0"),
        ("PROVIDER_TIMEOUT_SECONDS", "301"),
        ("RATE_LIMIT_REQUESTS", "0"),
        ("RATE_LIMIT_REQUESTS", "1.5"),
        ("RATE_LIMIT_WINDOW_SECONDS", "0"),
        ("RATE_LIMIT_WINDOW_SECONDS", "-10"),
    ],
)
def test_invalid_reliability_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert name.lower() in str(exc_info.value).lower() or "RETRY_MAX_DELAY" in str(exc_info.value)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.usefixtures("clean_env")
def test_provider_defaults_without_env() -> None:
    settings = Settings(_env_file=None)
    assert settings.groq_api_key is None
    assert settings.google_api_key is None
    assert settings.groq_model_name == "llama-3.3-70b-versatile"
    assert settings.gemini_model_name == "gemini-2.5-flash"


@pytest.mark.usefixtures("clean_env")
def test_provider_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL_NAME", "custom-groq-model")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("GEMINI_MODEL_NAME", "custom-gemini-model")

    settings = Settings(_env_file=None)

    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == "test-groq-key"
    assert settings.groq_model_name == "custom-groq-model"
    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "test-google-key"
    assert settings.gemini_model_name == "custom-gemini-model"


@pytest.mark.usefixtures("clean_env")
def test_empty_key_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert Settings(_env_file=None).groq_api_key is None


@pytest.mark.usefixtures("clean_env")
def test_api_keys_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    assert "test-groq-key" not in repr(Settings(_env_file=None))


@pytest.mark.usefixtures("clean_env")
def test_auth_settings_default_to_unset() -> None:
    settings = Settings(_env_file=None)
    assert settings.api_key_pepper is None


@pytest.mark.usefixtures("clean_env")
def test_auth_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY_PEPPER", "pepper-value-for-test-only")
    assert "pepper-value-for-test-only" not in repr(Settings(_env_file=None))


@pytest.mark.usefixtures("clean_env")
def test_persistence_and_cache_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./data/gateway.db"
    assert settings.cache_enabled is False
    assert settings.cache_ttl_seconds == 300
    assert settings.cache_max_entries == 1000


@pytest.mark.usefixtures("clean_env")
def test_persistence_and_cache_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./other/place.db")
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("CACHE_MAX_ENTRIES", "50")

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite:///./other/place.db"
    assert settings.cache_enabled is True
    assert settings.cache_ttl_seconds == 60
    assert settings.cache_max_entries == 50


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "postgresql://user@host/db"),
        ("DATABASE_URL", "sqlite:///"),
        ("DATABASE_URL", "sqlite:///:memory:"),
        ("DATABASE_URL", "./data/gateway.db"),
        ("CACHE_ENABLED", "maybe"),
        ("CACHE_TTL_SECONDS", "0"),
        ("CACHE_TTL_SECONDS", "-5"),
        ("CACHE_MAX_ENTRIES", "0"),
        ("CACHE_MAX_ENTRIES", "1000001"),
    ],
)
def test_invalid_persistence_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)


def test_app_starts_without_provider_keys(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    response = client.post(
        "/v1/chat/completions",
        json={"model": "mock", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 200
