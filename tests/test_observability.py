"""Request IDs, structured JSON logs, and the guarantee that logs never carry secrets."""

import json
import logging
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.api.routes import get_inference_service
from gateway.auth.models import IssuedApiKey
from gateway.main import app
from gateway.middleware import SECURITY_HEADERS
from gateway.observability.context import request_id_var
from gateway.observability.log_config import JsonFormatter, configure_logging
from gateway.providers.base import ProviderRequest, ProviderResponse
from gateway.services.inference import InferenceService
from gateway.services.usage import InMemoryUsageRecorder
from tests.conftest import secret_of

URL = "/v1/chat/completions"
REQUEST_ID = re.compile(r"req_[0-9a-f]{32}")
PROMPT_CANARY = "prompt-canary-must-never-be-logged"


def _payload(content: str = "Hi") -> dict[str, Any]:
    return {"model": "mock", "messages": [{"role": "user", "content": content}]}


def _formatted(caplog: pytest.LogCaptureFixture) -> str:
    """The gateway's own log output (the test client's httpx logs are not server logs)."""
    formatter = JsonFormatter()
    return "\n".join(formatter.format(r) for r in caplog.records if r.name.startswith("gateway"))


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "gateway.access"]


# --- request IDs -------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("get", "/health", {}, 200),
        ("post", URL, {"json": _payload()}, 401),
        ("get", "/does-not-exist", {}, 404),
        ("get", URL, {}, 405),
    ],
)
def test_every_response_has_a_request_id(
    anon_client: TestClient, method: str, path: str, kwargs: dict[str, Any], status: int
) -> None:
    response = getattr(anon_client, method)(path, **kwargs)
    assert response.status_code == status
    assert REQUEST_ID.fullmatch(response.headers["X-Request-ID"])


def test_request_ids_are_unique(anon_client: TestClient) -> None:
    ids = {anon_client.get("/health").headers["X-Request-ID"] for _ in range(20)}
    assert len(ids) == 20


@pytest.mark.parametrize("supplied", ["req_" + "a" * 32, "attacker-chosen", "x" * 10_000, "req_\r\nInjected: 1"])
def test_client_supplied_request_id_is_ignored(anon_client: TestClient, supplied: str) -> None:
    headers = {"X-Request-ID": supplied} if "\r" not in supplied else {}
    response = anon_client.get("/health", headers=headers)
    returned = response.headers["X-Request-ID"]
    assert returned != supplied
    assert REQUEST_ID.fullmatch(returned)


def test_completion_id_and_usage_record_use_the_request_id(
    client: TestClient, usage_recorder: InMemoryUsageRecorder
) -> None:
    response = client.post(URL, json=_payload())

    request_id = response.headers["X-Request-ID"]
    assert response.json()["id"] == request_id
    assert [r.request_id for r in usage_recorder.records] == [request_id]


def test_validation_and_limit_errors_carry_request_id(client: TestClient) -> None:
    invalid = client.post(URL, json={"model": "mock", "messages": []})
    too_large = client.post(URL, content=b"x" * (1024 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert invalid.status_code == 422 and REQUEST_ID.fullmatch(invalid.headers["X-Request-ID"])
    assert too_large.status_code == 413 and REQUEST_ID.fullmatch(too_large.headers["X-Request-ID"])


# --- unhandled errors ----------------------------------------------------------


class _Exploding:
    name = "exploding"
    model = "exploding"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("boom at C:\\srv\\secret\\path.py with token sk-leaky-value")


def test_unhandled_error_is_generic_and_logged_without_message(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app.dependency_overrides[get_inference_service] = lambda: InferenceService(lambda _: _Exploding())

    response = client.post(URL, json=_payload())

    assert response.status_code == 500
    assert response.json() == {"error": {"type": "internal_error", "message": "An unexpected error occurred"}}
    assert REQUEST_ID.fullmatch(response.headers["X-Request-ID"])
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    for leaked in ("boom", "secret", "sk-leaky-value", "Traceback", ".py"):
        assert leaked not in response.text

    output = _formatted(caplog)
    error_lines = [json.loads(line) for line in output.splitlines() if '"exc_type"' in line]
    assert error_lines and error_lines[0]["exc_type"] == "RuntimeError"
    assert any("inference.py" in frame for frame in error_lines[0]["exc_frames"])
    assert "sk-leaky-value" not in output
    assert "boom" not in output


# --- structured access logs ----------------------------------------------------


def test_access_log_has_structured_fields(client: TestClient, issued_key: IssuedApiKey, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    response = client.post(URL, json=_payload())

    [record] = _access_records(caplog)
    assert record.request_id == response.headers["X-Request-ID"]
    assert (record.method, record.route, record.status_code) == ("POST", URL, 200)
    assert (record.client_id, record.key_id) == ("test-client", issued_key.record.key_id)
    assert (record.provider, record.model, record.cache_status) == ("mock", "mock", "BYPASS")
    assert record.latency_ms >= 0


def test_access_log_uses_route_template_not_raw_path(anon_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    anon_client.get("/gw_live_invalid_test_key/secret-looking-path")

    [record] = _access_records(caplog)
    assert record.route == "unmatched"
    assert "secret-looking-path" not in _formatted(caplog)


def test_json_formatter_emits_one_json_object_per_record(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    client.post(URL, json=_payload())

    for line in _formatted(caplog).splitlines():
        entry = json.loads(line)
        assert {"timestamp", "level", "logger", "message"} <= set(entry)
        assert entry["timestamp"].endswith("+00:00")


def test_formatter_drops_non_allowlisted_extra_fields() -> None:
    record = logging.LogRecord("gateway.test", logging.INFO, __file__, 1, "hello", None, None)
    record.authorization = "Bearer gw_live_invalid_test_key"
    record.client_id = "c1"

    entry = json.loads(JsonFormatter().format(record))

    assert entry["client_id"] == "c1"
    assert "authorization" not in entry
    assert "gw_live" not in json.dumps(entry)


def test_formatter_adds_request_id_from_request_context() -> None:
    record = logging.LogRecord("gateway.test", logging.INFO, __file__, 1, "x", None, None)
    token = request_id_var.set("req_" + "b" * 32)
    try:
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["request_id"] == "req_" + "b" * 32
    assert "request_id" not in json.loads(JsonFormatter().format(record))


def test_configure_logging_is_idempotent() -> None:
    logger = logging.getLogger("gateway")
    configure_logging("DEBUG")
    configure_logging("WARNING")
    try:
        ours = [h for h in logger.handlers if h.get_name() == "gateway-json"]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, JsonFormatter)
        assert logger.level == logging.WARNING
    finally:
        for handler in [h for h in logger.handlers if h.get_name() == "gateway-json"]:
            logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)


# --- secrets never reach logs ----------------------------------------------------


def test_no_secrets_in_logs_across_success_and_failure_paths(
    client: TestClient, anon_client: TestClient, issued_key: IssuedApiKey, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client.post(URL, json=_payload(PROMPT_CANARY))
    client.post(URL, json={"model": "mock", "messages": [{"role": "robot", "content": PROMPT_CANARY}]})
    anon_client.post(URL, json=_payload(PROMPT_CANARY), headers={"Authorization": f"Bearer {issued_key.api_key}x"})
    anon_client.post(URL, json=_payload(PROMPT_CANARY), headers={"Authorization": "Bearer gw_live_invalid_test_key"})
    anon_client.post(URL, json=_payload(PROMPT_CANARY), headers={"Authorization": f"Basic {issued_key.api_key}"})

    output = _formatted(caplog) + "\n" + caplog.text
    assert _access_records(caplog), "expected access log lines"
    for forbidden in (issued_key.api_key, secret_of(issued_key.api_key), issued_key.record.key_hash, "Bearer", "Authorization", PROMPT_CANARY, "Mock response"):
        assert forbidden not in output
