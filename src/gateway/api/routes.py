from typing import Annotated

from fastapi import APIRouter, Depends

from gateway.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
)
from gateway.api.security import authenticate_request
from gateway.config import Settings, get_settings
from gateway.providers.factory import get_provider
from gateway.services.inference import InferenceService

router = APIRouter()
# Every /v1 route requires a valid API key; /health stays public on the root router.
v1_router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate_request)])


def get_inference_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> InferenceService:
    return InferenceService(lambda model: get_provider(model, settings))


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
