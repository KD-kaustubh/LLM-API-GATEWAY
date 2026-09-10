import pytest

from gateway.config import Settings
from gateway.errors import ProviderNotConfiguredError, UnsupportedModelError
from gateway.providers.factory import get_provider
from gateway.providers.gemini import GeminiProvider
from gateway.providers.groq import GroqProvider
from gateway.providers.mock import MockProvider


def _configured() -> Settings:
    return Settings(
        _env_file=None,
        groq_api_key="test-groq-key",
        groq_model_name="groq-model",
        google_api_key="test-google-key",
        gemini_model_name="gemini-model",
    )


def test_mock_needs_no_credentials(settings: Settings) -> None:
    assert isinstance(get_provider("mock", settings), MockProvider)


@pytest.mark.parametrize(("model", "cls"), [("groq", GroqProvider), ("gemini", GeminiProvider)])
def test_real_providers_resolved_when_configured(model: str, cls: type) -> None:
    provider = get_provider(model, _configured())
    assert isinstance(provider, cls)
    assert provider.name == model


def test_configured_model_name_is_used() -> None:
    assert get_provider("groq", _configured())._model == "groq-model"
    assert get_provider("gemini", _configured())._model == "gemini-model"


@pytest.mark.parametrize("model", ["groq", "gemini"])
def test_configured_timeout_is_passed_to_provider(model: str) -> None:
    settings = _configured().model_copy(update={"provider_timeout_seconds": 7.5})
    assert get_provider(model, settings)._timeout_seconds == 7.5


@pytest.mark.parametrize(("model", "env_var"), [("groq", "GROQ_API_KEY"), ("gemini", "GOOGLE_API_KEY")])
def test_missing_credentials_raise(settings: Settings, model: str, env_var: str) -> None:
    with pytest.raises(ProviderNotConfiguredError, match=env_var):
        get_provider(model, settings)


@pytest.mark.parametrize("model", ["gpt-4", "Mock", "groq/llama", "unknown"])
def test_unsupported_model_raises(settings: Settings, model: str) -> None:
    with pytest.raises(UnsupportedModelError, match="Supported: gemini, groq, mock"):
        get_provider(model, settings)
