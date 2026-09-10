import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings

PROVIDER_ENV_VARS = (
    "GROQ_API_KEY",
    "GROQ_MODEL_NAME",
    "GOOGLE_API_KEY",
    "GEMINI_MODEL_NAME",
    "API_KEY_PEPPER",
    "API_KEY_HASHES",
)


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
    assert settings.api_key_hashes is None


@pytest.mark.usefixtures("clean_env")
def test_auth_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY_PEPPER", "pepper-value-for-test-only")
    monkeypatch.setenv("API_KEY_HASHES", "dev:hash-entry-for-test-only")
    text = repr(Settings(_env_file=None))
    assert "pepper-value-for-test-only" not in text
    assert "hash-entry-for-test-only" not in text


def test_app_starts_without_provider_keys(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    response = client.post(
        "/v1/chat/completions",
        json={"model": "mock", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 200
