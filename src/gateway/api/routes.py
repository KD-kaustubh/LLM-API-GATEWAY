from typing import Annotated

from fastapi import APIRouter, Depends

from gateway.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
)
from gateway.api.security import enforce_rate_limit
from gateway.config import Settings, get_settings
from gateway.providers.factory import get_provider
from gateway.services.inference import InferenceService
from gateway.services.retry import Retrier, RetryPolicy

router = APIRouter()
# Every /v1 route is authenticated, then rate limited per client; /health stays public.
v1_router = APIRouter(prefix="/v1", dependencies=[Depends(enforce_rate_limit)])


def get_inference_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> InferenceService:
    policy = RetryPolicy(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    return InferenceService(lambda model: get_provider(model, settings), Retrier(policy))


@router.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(status="ok", service="llm-api-gateway", version=settings.app_version)


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
    service: Annotated[InferenceService, Depends(get_inference_service)],
) -> ChatCompletionResponse:
    return service.create_chat_completion(request)


router.include_router(v1_router)
