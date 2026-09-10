from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and optional .env."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    app_name: str = "LLM API Gateway"
    app_version: str = "0.1.0"
    app_env: str = "development"
    log_level: str = "INFO"

    groq_api_key: SecretStr | None = None
    groq_model_name: str = "llama-3.3-70b-versatile"

    google_api_key: SecretStr | None = None
    gemini_model_name: str = "gemini-2.5-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()
