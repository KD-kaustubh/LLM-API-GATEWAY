from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth.models import AuthenticatedClient
from gateway.auth.service import ApiKeyService
from gateway.errors import AuthenticationError

# auto_error=False so missing or non-Bearer headers raise our generic AuthenticationError.
bearer_scheme = HTTPBearer(auto_error=False, description="Gateway API key: `Bearer gw_live_...`")


def get_api_key_service(request: Request) -> ApiKeyService:
    return request.app.state.api_key_service


def authenticate_request(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> AuthenticatedClient:
    if credentials is None:
        raise AuthenticationError()
    return service.authenticate(credentials.credentials)
