import logging
import time
from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.errors import error_response
from gateway.observability import metrics
from gateway.observability.context import new_request_id, request_id_var

MAX_REQUEST_BODY_BYTES = 1024 * 1024
_TOO_LARGE_MESSAGE = "Request body too large"

access_logger = logging.getLogger("gateway.access")
error_logger = logging.getLogger("gateway.errors")

# API responses are JSON with per-caller data: never sniff, frame, cache, or leak referrers.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# Loopback names are always accepted so local and container health checks keep working; they
# cannot be used for DNS rebinding because a browser never sends them for a foreign origin.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class RequestContextMiddleware:
    """Outermost middleware: request ID, security headers, access log, HTTP metrics, safe 500s.

    Every request gets a server-generated `req_<uuid>` ID; a client-supplied X-Request-ID is
    ignored so IDs stay unique and unforgeable. The ID is returned in the X-Request-ID header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            error_logger.error("Unhandled exception", exc_info=True)
            if response_started:
                raise
            response = error_response(500, "internal_error", "An unexpected error occurred")
            await response(scope, receive, send_with_headers)
        finally:
            self._record(scope, state, status_code, time.perf_counter() - started)
            request_id_var.reset(token)

    @staticmethod
    def _record(scope: Scope, state: dict, status_code: int, duration: float) -> None:
        route_obj = scope.get("route")
        # The route template (e.g. /v1/chat/completions), never the raw path: bounded for metrics
        # and free of anything a client might have put in the URL.
        route = getattr(route_obj, "path", None) or metrics.UNMATCHED_ROUTE
        method = metrics.method_label(scope["method"])
        metrics.HTTP_REQUESTS.labels(method, route, str(status_code)).inc()
        metrics.HTTP_LATENCY.labels(method, route).observe(duration)
        access_logger.info(
            "request completed",
            extra={
                "request_id": state.get("request_id"),
                "method": method,
                "route": route,
                "status_code": status_code,
                "latency_ms": round(duration * 1000, 2),
                "client_id": state.get("client_id"),
                "key_id": state.get("key_id"),
                "provider": state.get("provider"),
                "model": state.get("model"),
                "cache_status": state.get("cache_status"),
            },
        )


class TrustedHostMiddleware:
    """Reject requests whose Host header is not allowed, with the standard error envelope."""

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        hosts = {h.strip().lower() for h in allowed_hosts if h.strip()}
        self.allow_any = "*" in hosts
        self.exact = {h for h in hosts if not h.startswith("*.")} | set(LOOPBACK_HOSTS)
        self.suffixes = tuple(h[1:] for h in hosts if h.startswith("*."))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "").lower()
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if hostname in self.exact or (self.suffixes and hostname.endswith(self.suffixes)):
            await self.app(scope, receive, send)
            return
        response = error_response(400, "invalid_host", "Invalid host header")
        await response(scope, receive, send)


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
