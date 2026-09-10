"""Smoke-test a running gateway (local container or a public deployment). Standard library only.

Secrets are read from environment variables and are never printed:
  GATEWAY_API_KEY          a valid gateway API key (required for the authenticated checks)
  GATEWAY_REVOKED_API_KEY  optional: a key that should be rejected (e.g. removed from API_KEY_SEEDS)
  METRICS_TOKEN            optional: the deployment's metrics token

Usage:
  python scripts/smoke_test.py https://<service>.onrender.com [--rate-limit] [--provider groq]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


class Result:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1


def request(
    base: str, method: str, path: str, token: str | None = None, body: Any = None
) -> tuple[int, dict[str, str], bytes]:
    headers = {"User-Agent": "gateway-smoke-test"}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def chat(base: str, token: str | None, content: str, model: str = "mock", **extra: Any) -> tuple[int, dict[str, str], bytes]:
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return request(base, "POST", "/v1/chat/completions", token, body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--rate-limit", action="store_true", help="exhaust the key's rate limit (sends up to limit+1 requests)")
    parser.add_argument("--provider", choices=["groq", "gemini"], help="also send one real (billable) provider request")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    api_key = os.environ.get("GATEWAY_API_KEY")
    revoked_key = os.environ.get("GATEWAY_REVOKED_API_KEY")
    metrics_token = os.environ.get("METRICS_TOKEN")
    r = Result()

    # Public endpoints (the first request may wake a sleeping free instance).
    status, headers, body = request(base, "GET", "/health")
    r.check("GET /health -> 200", status == 200, str(status))
    r.check("X-Request-ID present", headers.get("x-request-id", "").startswith("req_"))
    for name, value in SECURITY_HEADERS.items():
        r.check(f"header {name}", headers.get(name) == value, headers.get(name, "missing"))
    r.check("no server banner", "server" not in headers or "uvicorn" not in headers["server"].lower(), headers.get("server", "none"))
    status, _, body = request(base, "GET", "/ready")
    r.check("GET /ready -> 200", status == 200, body.decode(errors="replace")[:120])
    for path in ("/docs", "/openapi.json"):
        status, _, _ = request(base, "GET", path)
        r.check(f"GET {path} disabled -> 404", status == 404, str(status))

    status, _, _ = request(base, "GET", "/metrics")
    r.check("GET /metrics without token -> 401/404", status in (401, 404), str(status))
    if metrics_token:
        status, _, body = request(base, "GET", "/metrics", metrics_token)
        r.check("GET /metrics with METRICS_TOKEN -> 200", status == 200 and b"gateway_http_requests_total" in body, str(status))

    # Authentication.
    status, headers, body = chat(base, None, "hello")
    r.check("inference without key -> 401", status == 401, str(status))
    r.check("401 has WWW-Authenticate: Bearer", headers.get("www-authenticate") == "Bearer")
    status, _, _ = chat(base, "gw_live_invalid_test_key", "hello")
    r.check("inference with invalid key -> 401", status == 401, str(status))
    if revoked_key:
        status, _, _ = chat(base, revoked_key, "hello")
        r.check("inference with revoked key -> 401", status == 401, str(status))
    if not api_key:
        print("SKIP  authenticated checks (GATEWAY_API_KEY not set)")
        return finish(r)

    status, headers, body = chat(base, api_key, "hello from the smoke test")
    payload = json.loads(body) if status == 200 else {}
    r.check("inference with valid key (mock) -> 200", status == 200, str(status))
    r.check("response id == X-Request-ID", payload.get("id") == headers.get("x-request-id"))
    r.check("normalized response shape", {"id", "object", "model", "provider", "content", "usage"} <= set(payload))
    for text in (api_key, api_key[25:]):
        r.check("response does not echo the API key", text not in body.decode(errors="replace"))

    # Cache: a unique deterministic request, sent twice.
    nonce = f"cache check {uuid.uuid4().hex}"
    _, first, _ = chat(base, api_key, nonce, temperature=0)
    _, second, second_body = chat(base, api_key, nonce, temperature=0)
    r.check("cache: first request MISS", first.get("x-cache") == "MISS", first.get("x-cache", "missing"))
    r.check("cache: second request HIT", second.get("x-cache") == "HIT", second.get("x-cache", "missing"))
    if second.get("x-cache") == "HIT":
        r.check("cache hit reports zero tokens", json.loads(second_body)["usage"]["total_tokens"] == 0)

    if args.provider:
        started = time.monotonic()
        status, headers, body = chat(base, api_key, "Reply with one short sentence.", model=args.provider, max_tokens=30)
        elapsed = time.monotonic() - started
        payload = json.loads(body) if status == 200 else json.loads(body or b"{}")
        if status == 200:
            usage = payload["usage"]
            r.check(
                f"real provider {args.provider} -> 200",
                payload.get("provider") == args.provider,
                f"model={payload.get('model')} tokens={usage.get('total_tokens')} {elapsed:.1f}s, content {len(payload.get('content', ''))} chars",
            )
        else:
            r.check(f"real provider {args.provider} -> 200", False, f"{status} {payload.get('error', {}).get('type')}")

    if args.rate_limit:
        # Tokens refill while requests are in flight, so send until the first 429 (capped).
        _, headers, _ = chat(base, api_key, "rate limit probe")
        remaining = int(headers.get("x-ratelimit-remaining", "0"))
        allowed, status = 0, 200
        for _ in range(200):
            status, headers, _ = chat(base, api_key, "rate limit probe")
            if status != 200:
                break
            allowed += 1
        r.check("rate limit: at least the advertised remaining requests allowed", allowed >= remaining, f"remaining={remaining} allowed={allowed}")
        r.check("rate limit: then 429", status == 429, str(status))
        r.check("429 has Retry-After", headers.get("retry-after", "").isdigit(), headers.get("retry-after", "missing"))

    return finish(r)


def finish(r: Result) -> int:
    print(f"RESULT: {r.passed} passed, {r.failed} failed")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
