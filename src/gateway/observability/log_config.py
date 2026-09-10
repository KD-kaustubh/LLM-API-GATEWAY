import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

from gateway.observability.context import request_id_var

# Only these `extra=` fields are ever emitted, so arbitrary data passed to a logger (or added
# by a third-party library) cannot leak into the output.
SAFE_FIELDS = (
    "request_id",
    "client_id",
    "key_id",
    "method",
    "route",
    "status_code",
    "latency_ms",
    "provider",
    "model",
    "cache_status",
    "error_type",
    "reason",
    "attempt",
    "max_attempts",
    "retry_in_s",
)

_HANDLER_NAME = "gateway-json"


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Exceptions are reduced to their type and code locations."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if "request_id" not in entry:
            # Handlers emit synchronously in the logging thread, so the request context is current.
            request_id = request_id_var.get()
            if request_id is not None:
                entry["request_id"] = request_id
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _, tb = record.exc_info
            # Exception messages can carry request data or secrets; only the type and the
            # file:line:function frames are logged.
            entry["exc_type"] = exc_type.__name__
            entry["exc_frames"] = [
                f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(tb)[-10:]
            ]
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Attach a JSON stdout handler to the `gateway` logger. Safe to call repeatedly."""
    logger = logging.getLogger("gateway")
    for handler in [h for h in logger.handlers if h.get_name() == _HANDLER_NAME]:
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
