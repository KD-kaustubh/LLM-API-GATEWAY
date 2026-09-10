from fastapi import APIRouter

from gateway.api.schemas import HealthResponse
from gateway.config import settings

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="llm-api-gateway",
        version=settings.app_version,
    )
