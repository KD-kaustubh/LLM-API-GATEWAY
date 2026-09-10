from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "LLM API Gateway"
    app_version: str = "0.1.0"
    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
