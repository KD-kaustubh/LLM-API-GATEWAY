from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.persistence.database import sqlite_path_from_url


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

    api_key_pepper: SecretStr | None = None

    database_url: str = "sqlite:///./data/gateway.db"

    cache_enabled: bool = False
    cache_ttl_seconds: float = Field(default=300.0, gt=0, le=7 * 86_400)
    cache_max_entries: int = Field(default=1000, ge=1, le=100_000)

    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_delay: float = Field(default=0.5, ge=0, le=30)
    retry_max_delay: float = Field(default=4.0, ge=0, le=60)

    rate_limit_requests: int = Field(default=60, ge=1, le=100_000)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0, le=86_400)

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        sqlite_path_from_url(value)
        return value

    @model_validator(mode="after")
    def _check_retry_delays(self) -> "Settings":
        if self.retry_max_delay < self.retry_base_delay:
            raise ValueError("RETRY_MAX_DELAY must be greater than or equal to RETRY_BASE_DELAY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
