from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.api.routes import router as api_router
from gateway.auth.bootstrap import build_api_key_service
from gateway.config import get_settings
from gateway.errors import GatewayError, error_response
from gateway.middleware import BodySizeLimitMiddleware

MAX_VALIDATION_DETAILS = 10
MAX_LOC_PART_LENGTH = 64

_HTTP_ERROR_TYPES = {404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}


async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
    return error_response(exc.status_code, exc.error_type, exc.message, headers=exc.headers)


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    error_type = _HTTP_ERROR_TYPES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, error_type, str(exc.detail), headers=exc.headers)


def _safe_loc(loc: tuple[object, ...]) -> list[object]:
    return [part if isinstance(part, int) else str(part)[:MAX_LOC_PART_LENGTH] for part in loc]


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Only location, message, and type are returned: never the submitted input or its context.
    details = [
        {"loc": _safe_loc(err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()[:MAX_VALIDATION_DETAILS]
    ]
    return error_response(422, "invalid_request", "Request validation failed", details=details)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(500, "internal_error", "An unexpected error occurred")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.state.api_key_service = build_api_key_service(settings)
    app.include_router(api_router)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_exception_handler(GatewayError, handle_gateway_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


app = create_app()
