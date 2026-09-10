import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from gateway.auth.models import AuthenticatedClient
from gateway.auth.service import ApiKeyService, reject_authentication
from gateway.config import Settings, get_settings
from gateway.errors import AuthenticationError, RateLimitExceededError
from gateway.observability import metrics
from gateway.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

# auto_error=False so missing or non-Bearer headers raise our generic AuthenticationError.
bearer_scheme = HTTPBearer(auto_error=False, description="Gateway API key: `Bearer gw_live_...`")


class MetricsAuthenticationError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__()
        self.message = "Invalid metrics token"


def get_api_key_service(request: Request) -> ApiKeyService:
    return request.app.state.api_key_service


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def authenticate_request(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> AuthenticatedClient:
    if credentials is None:
        reject_authentication("missing_credentials")
    client = service.authenticate(credentials.credentials)
    # Safe identifiers for the access log; the raw key never leaves this function.
    request.state.client_id = client.client_id
    request.state.key_id = client.key_id
    return client


def enforce_rate_limit(
    client: Annotated[AuthenticatedClient, Depends(authenticate_request)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    response: Response,
) -> AuthenticatedClient:
    """Runs after authentication; limits are keyed by client_id, never by the raw API key."""
    result = limiter.allow(client.client_id)
    if not result.allowed:
        metrics.RATE_LIMIT_REJECTIONS.inc()
        raise RateLimitExceededError(result.limit, result.retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    return client


def authorize_metrics(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """With METRICS_TOKEN set, require it; without one, /metrics exists only in development."""
    if settings.metrics_token is None:
        if not settings.is_development:
            raise HTTPException(status_code=404, detail="Not Found")
        return
    expected = settings.metrics_token.get_secret_value().encode()
    presented = credentials.credentials.encode() if credentials else b""
    if not hmac.compare_digest(presented, expected):
        metrics.AUTH_FAILURES.labels("metrics_token").inc()
        logger.info("Metrics authentication failed", extra={"reason": "metrics_token"})
        raise MetricsAuthenticationError()
