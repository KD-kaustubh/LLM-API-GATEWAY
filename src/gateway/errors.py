from fastapi.responses import JSONResponse


class GatewayError(Exception):
    """Base error whose message is safe to return to clients."""

    status_code: int = 500
    error_type: str = "internal_error"
    headers: dict[str, str] | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthenticationError(GatewayError):
    """Deliberately generic: never reveals whether a key was missing, unknown, or revoked."""

    status_code = 401
    error_type = "authentication_error"
    headers = {"WWW-Authenticate": "Bearer"}

    def __init__(self) -> None:
        super().__init__("Invalid API key")


class UnsupportedModelError(GatewayError):
    status_code = 400
    error_type = "unsupported_model"


class ProviderNotConfiguredError(GatewayError):
    status_code = 503
    error_type = "provider_not_configured"


class ProviderError(GatewayError):
    """Upstream provider failure. Not retried unless it is a TransientProviderError."""

    status_code = 502
    error_type = "provider_error"


class TransientProviderError(ProviderError):
    """Upstream failure that is likely to succeed if repeated (network, overload, timeout)."""


class ProviderTimeoutError(TransientProviderError):
    pass


class RateLimitExceededError(GatewayError):
    status_code = 429
    error_type = "rate_limit_error"

    def __init__(self, limit: int, retry_after_seconds: int) -> None:
        super().__init__("Rate limit exceeded")
        self.headers = {
            "Retry-After": str(retry_after_seconds),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        }


def error_response(
    status_code: int,
    error_type: str,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message, **extra}},
        headers=headers,
    )
