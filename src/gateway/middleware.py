from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.errors import error_response

MAX_REQUEST_BODY_BYTES = 1024 * 1024
_TOO_LARGE_MESSAGE = "Request body too large"


class BodySizeLimitMiddleware:
    """Reject bodies over the limit, by Content-Length up front or by counting streamed chunks."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope["headers"]).get(b"content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            response = error_response(413, "request_too_large", _TOO_LARGE_MESSAGE)
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=_TOO_LARGE_MESSAGE)
            return message

        await self.app(scope, limited_receive, send)
