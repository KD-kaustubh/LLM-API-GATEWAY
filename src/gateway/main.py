from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.api.routes import router as api_router
from gateway.config import get_settings
from gateway.errors import GatewayError


def _error_response(status_code: int, error_type: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message, **extra}},
    )


async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
    return _error_response(exc.status_code, exc.error_type, exc.message)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in exc.errors()
    ]
    return _error_response(422, "invalid_request", "Request validation failed", details=details)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(500, "internal_error", "An unexpected error occurred")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.include_router(api_router)
    app.add_exception_handler(GatewayError, handle_gateway_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


app = create_app()
