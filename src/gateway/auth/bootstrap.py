import logging
import secrets

from gateway.auth.hashing import MIN_PEPPER_BYTES, ApiKeyHasher
from gateway.auth.service import ApiKeyService
from gateway.auth.store import ApiKeyStore
from gateway.config import Settings

logger = logging.getLogger(__name__)


def hasher_from_settings(settings: Settings) -> ApiKeyHasher | None:
    if settings.api_key_pepper is None:
        return None
    return ApiKeyHasher(settings.api_key_pepper.get_secret_value().encode())


def build_api_key_service(settings: Settings, store: ApiKeyStore) -> ApiKeyService:
    hasher = hasher_from_settings(settings)
    if hasher is None:
        if settings.app_env == "production":
            raise ValueError("API_KEY_PEPPER must be set in production")
        logger.warning(
            "API_KEY_PEPPER is not set: using a random per-process pepper, so stored API keys "
            "cannot be verified and every authenticated request will be rejected"
        )
        hasher = ApiKeyHasher(secrets.token_bytes(MIN_PEPPER_BYTES))
    return ApiKeyService(store, hasher)
