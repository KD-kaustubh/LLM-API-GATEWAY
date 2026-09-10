from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from gateway.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
)
from gateway.api.security import authorize_metrics, enforce_rate_limit
from gateway.observability import metrics
from gateway.persistence.migrations import database_is_ready
from gateway.auth.models import AuthenticatedClient
from gateway.config import Settings, get_settings
from gateway.providers.factory import get_provider
from gateway.services.cache import CacheStore, ResponseCache
from gateway.services.inference import InferenceService
from gateway.services.retry import Retrier, RetryPolicy
from gateway.services.usage import UsageRecorder

router = APIRouter()
# Every /v1 route is authenticated, then rate limited per client; /health stays public.
v1_router = APIRouter(prefix="/v1", dependencies=[Depends(enforce_rate_limit)])


def get_usage_recorder(request: Request) -> UsageRecorder:
    return request.app.state.usage_recorder


def get_cache_store(request: Request) -> CacheStore:
    return request.app.state.cache_store


def get_response_cache(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CacheStore, Depends(get_cache_store)],
) -> ResponseCache | None:
    if not settings.cache_enabled:
        return None
    return ResponseCache(store, ttl_seconds=settings.cache_ttl_seconds)


def get_inference_service(
    settings: Annotated[Settings, Depends(get_settings)],
    cache: Annotated[ResponseCache | None, Depends(get_response_cache)],
    usage: Annotated[UsageRecorder, Depends(get_usage_recorder)],
) -> InferenceService:
    policy = RetryPolicy(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    return InferenceService(
        lambda model: get_provider(model, settings), Retrier(policy), cache=cache, usage=usage
    )


@router.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness: the process is serving HTTP. No I/O, no dependencies, no provider calls."""
    return HealthResponse(status="ok", service="llm-api-gateway", version=settings.app_version)


@router.get("/ready", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}})
def ready(request: Request, response: Response) -> ReadinessResponse:
    """Readiness: startup finished, the database is migrated and reachable, and API keys can be
    verified. Providers are not checked: an upstream outage must not take the gateway out."""
    state = request.app.state
    database = getattr(state, "database", None)
    checks: dict[str, str] = {
        "startup": "ok" if database is not None else "fail",
        "database": "ok" if database is not None and database_is_ready(database) else "fail",
        "authentication": "ok" if getattr(state, "api_keys_verifiable", False) else "fail",
    }
    is_ready = all(result == "ok" for result in checks.values())
    if not is_ready:
        response.status_code = 503
    return ReadinessResponse(status="ready" if is_ready else "not_ready", checks=checks)


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(authorize_metrics)])
def metrics_endpoint() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@v1_router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    client: Annotated[AuthenticatedClient, Depends(enforce_rate_limit)],
    service: Annotated[InferenceService, Depends(get_inference_service)],
    response: Response,
) -> ChatCompletionResponse:
    request_id = getattr(http_request.state, "request_id", None)
    result = service.create_chat_completion(request, client.client_id, client.key_id, request_id)
    response.headers["X-Cache"] = result.cache_status
    # Bounded, non-secret fields for the access log line.
    http_request.state.provider = result.response.provider
    http_request.state.model = result.response.model
    http_request.state.cache_status = result.cache_status
    return result.response


router.include_router(v1_router)
