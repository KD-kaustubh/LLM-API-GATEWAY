from contextvars import ContextVar
from uuid import uuid4

# Set by RequestContextMiddleware for the duration of each HTTP request; copied into the
# worker threads that run sync dependencies and endpoints.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid4().hex}"
