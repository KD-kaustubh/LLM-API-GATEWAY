from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth.models import AuthenticatedClient
from gateway.auth.service import ApiKeyService
from gateway.errors import AuthenticationError, RateLimitExceededError
from gateway.rate_limit import RateLimiter

# auto_error=False so missing or non-Bearer headers raise our generic AuthenticationError.
bearer_scheme = HTTPBearer(auto_error=False, description="Gateway API key: `Bearer gw_live_...`")


def get_api_key_service(request: Request) -> ApiKeyService:
    return request.app.state.api_key_service


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def authenticate_request(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> AuthenticatedClient:
    if credentials is None:
        raise AuthenticationError()
    return service.authenticate(credentials.credentials)


def enforce_rate_limit(
    client: Annotated[AuthenticatedClient, Depends(authenticate_request)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    response: Response,
) -> AuthenticatedClient:
    """Runs after authentication; limits are keyed by client_id, never by the raw API key."""
    result = limiter.allow(client.client_id)
    if not result.allowed:
        raise RateLimitExceededError(result.limit, result.retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    return client
