from collections.abc import Callable

from pydantic import SecretStr

from gateway.config import Settings
from gateway.errors import ProviderNotConfiguredError, UnsupportedModelError
from gateway.providers.base import LLMProvider
from gateway.providers.gemini import GeminiProvider
from gateway.providers.groq import GroqProvider
from gateway.providers.mock import MockProvider


def _require_key(key: SecretStr | None, env_var: str, provider: str) -> str:
    if key is None:
        raise ProviderNotConfiguredError(f"Provider '{provider}' is not configured: set {env_var}")
    return key.get_secret_value()


def _build_mock(settings: Settings) -> LLMProvider:
    return MockProvider()


def _build_groq(settings: Settings) -> LLMProvider:
    api_key = _require_key(settings.groq_api_key, "GROQ_API_KEY", "groq")
    return GroqProvider(
        api_key=api_key,
        model=settings.groq_model_name,
        timeout_seconds=settings.provider_timeout_seconds,
    )


def _build_gemini(settings: Settings) -> LLMProvider:
    api_key = _require_key(settings.google_api_key, "GOOGLE_API_KEY", "gemini")
    return GeminiProvider(
        api_key=api_key,
        model=settings.gemini_model_name,
        timeout_seconds=settings.provider_timeout_seconds,
    )


PROVIDER_BUILDERS: dict[str, Callable[[Settings], LLMProvider]] = {
    "mock": _build_mock,
    "groq": _build_groq,
    "gemini": _build_gemini,
}


def get_provider(model: str, settings: Settings) -> LLMProvider:
    builder = PROVIDER_BUILDERS.get(model)
    if builder is None:
        supported = ", ".join(sorted(PROVIDER_BUILDERS))
        raise UnsupportedModelError(f"Unsupported model '{model}'. Supported: {supported}")
    return builder(settings)
