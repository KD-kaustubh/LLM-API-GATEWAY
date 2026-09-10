import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.api.routes import router as api_router
from gateway.auth.bootstrap import build_api_key_service
from gateway.config import get_settings
from gateway.errors import GatewayError, error_response
from gateway.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    TrustedHostMiddleware,
)
from gateway.observability.log_config import configure_logging
from gateway.persistence.migrations import initialize_database
from gateway.persistence.repositories import (
    SQLiteApiKeyStore,
    SQLiteCacheStore,
    SQLiteUsageRepository,
)
from gateway.rate_limit import InMemoryRateLimiter

logger = logging.getLogger("gateway")

MAX_VALIDATION_DETAILS = 10
MAX_LOC_PART_LENGTH = 64

_HTTP_ERROR_TYPES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "request_too_large",
}


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail closed: if the database cannot be initialized, the server does not start.
    database = initialize_database(settings.database_url)
    app.state.api_key_service = build_api_key_service(settings, SQLiteApiKeyStore(database))
    app.state.usage_recorder = SQLiteUsageRepository(database)
    app.state.cache_store = SQLiteCacheStore(database, max_entries=settings.cache_max_entries)
    app.state.api_keys_verifiable = settings.api_key_pepper is not None
    app.state.database = database  # set last: /ready reports "startup ok" only after all of the above
    logger.info("Gateway started (environment: %s)", settings.app_env)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    # Interactive docs and the OpenAPI schema are development conveniences, not production surface.
    docs = settings.is_development
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.rate_limiter = InMemoryRateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    app.include_router(api_router)
    # Added innermost first: RequestContext wraps everything so every response gets an ID,
    # security headers, an access-log line, and HTTP metrics.
    app.add_middleware(BodySizeLimitMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=[
                "X-Request-ID", "X-Cache", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining",
            ],
            max_age=600,
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(GatewayError, handle_gateway_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


app = create_app()
