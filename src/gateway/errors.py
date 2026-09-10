class GatewayError(Exception):
    """Base error whose message is safe to return to clients."""

    status_code: int = 500
    error_type: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedModelError(GatewayError):
    status_code = 400
    error_type = "unsupported_model"


class ProviderNotConfiguredError(GatewayError):
    status_code = 503
    error_type = "provider_not_configured"


class ProviderError(GatewayError):
    status_code = 502
    error_type = "provider_error"
