# LLM API Gateway

A production-style backend gateway that puts one authenticated, rate-limited, observable API
in front of multiple LLM providers (Groq and Google Gemini), with retries, response caching,
usage tracking, and SQLite persistence — packaged as a hardened Docker image.

It is built as a backend-engineering portfolio project: small enough to read end to end,
but with the concerns a real gateway has to get right — credential handling, failure
isolation, bounded resources, and safe logging.

**Live deployment:** not yet deployed. See [Deployment](#16-deployment) for the Render
Blueprint and the smoke test used to verify a deployment.

---

## Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Features](#3-features)
4. [Tech stack](#4-tech-stack)
5. [API endpoints](#5-api-endpoints)
6. [Authentication](#6-authentication)
7. [Reliability](#7-reliability)
8. [Rate limiting](#8-rate-limiting)
9. [Caching](#9-caching)
10. [Usage tracking](#10-usage-tracking)
11. [Observability](#11-observability)
12. [Security](#12-security)
13. [Local development](#13-local-development)
14. [Docker](#14-docker)
15. [Environment variables](#15-environment-variables)
16. [Deployment](#16-deployment)
17. [Production limitations](#17-production-limitations)
18. [Example requests](#18-example-requests)
19. [Example response](#19-example-response)
20. [Project structure](#20-project-structure)
21. [Testing](#21-testing)
22. [Future improvements](#22-future-improvements)

---

## 1. Overview

Clients call one endpoint, `POST /v1/chat/completions`, with a gateway API key and a
provider-agnostic request. The gateway authenticates the key, applies a per-client rate limit,
validates the request, serves it from cache when that is safe, otherwise calls the selected
provider with timeouts and bounded retries, records usage, and returns a normalized response.

| `model`  | Provider | Upstream model (configurable) | Needs            |
|----------|----------|-------------------------------|------------------|
| `mock`   | Mock     | —                             | nothing          |
| `groq`   | Groq     | `GROQ_MODEL_NAME`             | `GROQ_API_KEY`   |
| `gemini` | Gemini   | `GEMINI_MODEL_NAME`           | `GOOGLE_API_KEY` |

The mock provider is deterministic and offline, so the whole gateway can be run and tested
without any provider account.

## 2. Architecture

```
Client ──HTTPS──▶ (platform TLS termination) ──▶ Docker container: Uvicorn + FastAPI
                                                       │
 ┌─────────────────────────────────────────────────────┴───────────────────────────────┐
 │ Request context middleware   request ID, security headers, JSON access log, metrics │
 │ Trusted host middleware      → 400                                                   │
 │ CORS middleware              only when CORS_ORIGINS is set                           │
 │ Body size limit              → 413                                                   │
 │ API-key authentication       → 401                                                   │
 │ Per-client rate limit        → 429                                                   │
 │ Request validation           → 422                                                   │
 │ Inference service                                                                    │
 │   ├─ provider factory        → 400 unsupported model / 503 provider not configured   │
 │   ├─ response cache lookup   temperature 0 only → HIT skips the provider             │
 │   ├─ retrier                 transient errors only, exponential backoff + jitter     │
 │   │    └─ provider           Mock / Groq / Gemini, per-attempt timeout → 502         │
 │   ├─ cache store             successful responses only                               │
 │   └─ usage record            every successful completion                             │
 └──────────────────────────────────────────────────────────────────────────────────────┘
                                                       │
                              SQLite: api_keys · usage_records · cache_entries
```

Layering is enforced by module boundaries:

- `gateway/api` — routes, schemas, and FastAPI dependencies. No provider or SQL code.
- `gateway/auth` — key generation, hashing, verification, and the `ApiKeyStore` protocol.
  Knows nothing about providers or inference.
- `gateway/services` — the provider-independent inference flow, retries, the cache, and the
  `UsageRecorder` / `CacheStore` protocols. Knows nothing about how keys are stored.
- `gateway/providers` — the `LLMProvider` protocol and one module per SDK; SDK imports never
  leave their module.
- `gateway/persistence` — all SQL: connections, versioned migrations, and SQLite
  implementations of the store protocols.
- `gateway/observability` — request-ID context, the JSON log formatter, and metrics.

## 3. Features

- **Unified API** for Groq, Gemini, and a mock provider, with a normalized response shape.
- **API-key authentication** — HMAC-SHA256 hashed keys, timing-safe verification, generic
  errors, CLI key management, revocation.
- **Strict validation** — bounded models, messages, content, `temperature`, `max_tokens`,
  and a 1 MiB body limit.
- **Reliability** — per-attempt provider timeouts; bounded retries with backoff for
  transient failures only.
- **Rate limiting** — per-client token bucket with `Retry-After`.
- **Response cache** — opt-in, deterministic requests only, TTL and size bounded.
- **Usage tracking** — one row per completion: tokens, latency, cache hit.
- **Observability** — JSON logs with request IDs, Prometheus metrics, liveness and readiness.
- **Security hardening** — security headers, trusted hosts, opt-in CORS, production lockdown
  of docs and metrics, no secrets in logs, errors, metrics, or the image.
- **Container** — two-stage, non-root Docker image with a health check; Compose file; Render
  Blueprint.

## 4. Tech stack

Python 3.12 · FastAPI · Pydantic / pydantic-settings · Uvicorn · SQLite (`sqlite3`) ·
Groq SDK · Google Gen AI SDK · `prometheus-client` · Docker · pytest.

Authentication, caching, persistence, and JSON logging use only the standard library.

## 5. API endpoints

| Method & path                | Access | Purpose |
|------------------------------|--------|---------|
| `POST /v1/chat/completions`  | API key | Chat completion through the gateway |
| `GET /health`                | Public | Liveness: the process serves HTTP. No I/O. |
| `GET /ready`                 | Public | Readiness: startup done, database reachable and migrated, keys verifiable |
| `GET /metrics`               | `METRICS_TOKEN` | Prometheus metrics (open only in development when no token is set) |
| `GET /docs`, `/redoc`, `/openapi.json` | Development only | Interactive API docs; `404` in production |

### `POST /v1/chat/completions`

**Headers:** `Authorization: Bearer <gateway API key>`, `Content-Type: application/json`.

**Request body** (unknown fields are rejected):

| Field         | Type   | Rules |
|---------------|--------|-------|
| `model`       | string | Required, 1–100 chars: `mock`, `groq`, or `gemini` |
| `messages`    | array  | Required, 1–100 items of `{role, content}` |
| `role`        | string | `system`, `user`, or `assistant` |
| `content`     | string | Required, 1–100,000 chars, must contain non-whitespace |
| `temperature` | number | Optional, `0.0`–`2.0` (`0` makes the request cacheable) |
| `max_tokens`  | int    | Optional, `1`–`32768` |

**Response headers:**

| Header | Meaning |
|--------|---------|
| `X-Request-ID` | `req_<32 hex>`, generated by the gateway for every response (errors included) |
| `X-Cache` | `HIT`, `MISS` (cacheable, not found), or `BYPASS` (cache off or request not cacheable) |
| `X-RateLimit-Limit`, `X-RateLimit-Remaining` | The caller's own rate-limit state |
| `Retry-After` | On `429`: seconds until the next request is allowed |

**Errors** share one envelope, `{"error": {"type": "...", "message": "..."}}`:

| Status | `type` | When |
|--------|--------|------|
| 400 | `unsupported_model` | `model` is not `mock`, `groq`, or `gemini` |
| 400 | `invalid_host` | `Host` header not trusted |
| 401 | `authentication_error` | Missing, malformed, unknown, or revoked key (always the same body) |
| 404 | `not_found` | Unknown route |
| 405 | `method_not_allowed` | Wrong HTTP method |
| 413 | `request_too_large` | Body over 1 MiB |
| 422 | `invalid_request` | Validation failed; `details` lists field location, message, and type (max 10), never submitted values |
| 429 | `rate_limit_error` | Rate limit exceeded |
| 502 | `provider_error` | Provider failed, timed out, or returned nothing (after any retries) |
| 503 | `provider_not_configured` | The chosen provider has no API key configured |
| 500 | `internal_error` | Unexpected error; no internals are exposed |

### `GET /health` and `GET /ready`

```json
{"status": "ok", "service": "llm-api-gateway", "version": "0.1.0"}
{"status": "ready", "checks": {"startup": "ok", "database": "ok", "authentication": "ok"}}
```

`/ready` returns `503` with `"status": "not_ready"` and the failing check if startup has not
completed, the SQLite schema cannot be read (checked with one query and a 1-second timeout,
never creating a file), or `API_KEY_PEPPER` is missing. Neither endpoint calls a provider, so
a provider outage returns `502` on completions but leaves the gateway live and ready.

### `GET /metrics`

With `METRICS_TOKEN` set, requires `Authorization: Bearer <METRICS_TOKEN>` (compared in
constant time; a gateway API key does not work). Without a token it is open only when
`APP_ENV=development` and returns `404` otherwise. See [Observability](#11-observability).

## 6. Authentication

Keys look like `gw_live_<16 hex key id>_<43-char secret>` (256 bits of randomness from
`secrets`). Only the key ID, an HMAC-SHA256 hash keyed by the server-side `API_KEY_PEPPER`,
the client ID, and timestamps are stored — never the raw key or the pepper.

- Verification uses `hmac.compare_digest`, and unknown key IDs are still compared against a
  dummy hash so timing does not reveal which IDs exist.
- Every failure returns the same `401` with `WWW-Authenticate: Bearer`.
- Failures are logged and counted with a reason (`missing_credentials`, `malformed_key`,
  `unknown_key`, `hash_mismatch`, `revoked_key`) and the non-secret key ID only.
- `APP_ENV=production` refuses to start without `API_KEY_PEPPER`; changing the pepper
  invalidates every key.
- If the credential store cannot be read, the request fails closed with a generic `500`.

**Key management** is CLI-only (no key-management HTTP endpoints). Both commands use the
database from `DATABASE_URL` and need `API_KEY_PEPPER` in the environment:

```
gateway-create-key --client-id my-client          # stores the hash, prints the key once
gateway-revoke-key --key-id <16-hex key id>         # immediate, persistent revocation
gateway-create-key --client-id my-client --seed   # stores nothing; prints the key once
                                                  # and a hash-only API_KEY_SEEDS entry
```

`API_KEY_SEEDS` (comma-separated `client_id:key_id:key_hash` entries) is for hosts without a
shell or persistent disk: at startup, any entry whose key ID is not in the database is
inserted. Existing rows are never modified, so a revoked key stays revoked. Seeds only work
with the same pepper that created them, and malformed entries stop startup.

## 7. Reliability

- **Timeouts:** every provider attempt uses `PROVIDER_TIMEOUT_SECONDS` (default 30) through
  each SDK's own timeout option. Both SDKs' built-in retries are disabled, so the gateway is
  the only retry layer.
- **Retries:** only failures classified as transient are retried — timeouts, connection
  errors, and upstream `408`, `429`, `502`, `503`, `504`. Everything else (validation,
  authentication, unsupported model, missing credentials, other upstream statuses including
  `500`, empty responses, unexpected exceptions) fails on the first attempt, because a retried
  completion can be billed twice.
- **Bounded:** at most `MAX_RETRIES + 1` attempts (default 3). Before retry *n* the gateway
  waits 50–100% of `min(RETRY_MAX_DELAY, RETRY_BASE_DELAY × 2^(n-1))`.
- Provider resolution (unknown model, missing key) happens once, outside the retry loop. When
  retries are exhausted the client gets `502 provider_error` with a gateway-written message;
  SDK exception text is never returned or logged.

## 8. Rate limiting

A per-client **token bucket**, checked after authentication and before validation, keyed by
`client_id` (all of a client's keys share it; raw keys are never used as identifiers). Each
client can burst up to `RATE_LIMIT_REQUESTS` and refills at that many per
`RATE_LIMIT_WINDOW_SECONDS` (default 60/60 s). `/health`, `/ready`, and `/metrics` are not
rate limited, and requests failing authentication never touch a client's bucket. State is
thread-safe and pruned when idle; it is in memory, per process.

## 9. Caching

Off by default (`CACHE_ENABLED=false`). When enabled:

- **Only safe requests are cached:** `temperature` explicitly `0` (an omitted temperature
  uses the provider's non-zero default). Errors, timeouts, exhausted retries, empty
  responses, and rejected requests are never cached; a retried request is written once.
- **Key:** SHA-256 of canonical JSON of the key version, provider, configured upstream model,
  `model`, every message, `temperature`, and `max_tokens` — never credentials, identity, or
  request IDs. Changing `GROQ_MODEL_NAME` therefore never serves the old model's answers.
- **Shared across clients:** identical requests share an entry, which is safe because
  providers are stateless with respect to the caller.
- **Bounded:** entries expire after `CACHE_TTL_SECONDS` (300), expired rows are deleted on
  every write, at most `CACHE_MAX_ENTRIES` (1000) rows are kept, and responses over 64 KiB are
  not cached.
- **Hits** skip the provider, return the original content with a new `id` and zero `usage`,
  set `X-Cache: HIT`, and still consume a rate-limit token.
- Cache read and write failures fail open (logged; the request proceeds).

## 10. Usage tracking

Every successful completion writes one `usage_records` row: request ID (the response `id`),
client ID, key ID, model, provider, UTC timestamp, input/output/total tokens, latency, and
`cache_hit`. Token counts come from the provider; if a provider does not report one it is
stored as `NULL`, never estimated (mock counts are documented word counts). Cache hits are
recorded with `provider = "cache"` and zero tokens. Rejected and failed requests are not
recorded. A usage-write failure is logged and the successful response is still returned.

## 11. Observability

**Request IDs.** Every request gets a server-generated `req_<uuid>` — returned in
`X-Request-ID`, used as the completion `id` and usage `request_id`, and attached to every
log line. Client-supplied `X-Request-ID` values are ignored, so IDs cannot be forged or used
to inject text into logs.

**Structured logs.** JSON lines on stdout at `LOG_LEVEL`:

```json
{"timestamp": "2026-09-10T10:02:10.522+00:00", "level": "INFO", "logger": "gateway.access", "message": "request completed", "request_id": "req_62ee…", "method": "POST", "route": "/v1/chat/completions", "status_code": 200, "latency_ms": 12.4, "client_id": "team-a", "key_id": "1f0e…", "provider": "mock", "model": "mock", "cache_status": "HIT"}
```

The formatter writes only an allow-list of fields and logs the route template, never the raw
path. Keys, hashes, `Authorization` headers, peppers, provider keys, prompts, and responses
are never logged. Unhandled exceptions are logged as type plus `file:line:function` frames,
without the message. Uvicorn's own start/stop lines remain plain text.

**Metrics** (Prometheus text format; every label comes from a fixed set — unknown routes are
`unmatched`, unknown methods `OTHER`, and there are no per-request, per-client, per-key, or
per-model labels):

| Metric | Labels |
|--------|--------|
| `gateway_http_requests_total`, `gateway_http_request_duration_seconds` | `method`, `route` (+ `status`) |
| `gateway_auth_failures_total` | `reason` |
| `gateway_rate_limit_rejections_total` | — |
| `gateway_inference_requests_total` | `outcome` |
| `gateway_cache_lookups_total` | `result` (`hit`, `miss`, `bypass`) |
| `gateway_completions_total` | `provider` (`cache` for hits) |
| `gateway_tokens_total` | `provider`, `direction` |
| `gateway_provider_calls_total`, `gateway_provider_errors_total` | `provider`, `outcome` / `error_type` |
| `gateway_provider_call_duration_seconds`, `gateway_provider_retries_total` | `provider` |

## 12. Security

- **Secrets** (`API_KEY_PEPPER`, `METRICS_TOKEN`, `API_KEY_SEEDS`, provider keys) come only
  from the environment, are masked in settings `repr`, and never appear in the image, logs,
  metrics, error responses, or the database file (verified by tests and container checks).
- **Response headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`; no `Server` banner. HSTS is left
  to the TLS terminator.
- **Trusted hosts:** `Host` must match `TRUSTED_HOSTS` (exact or `*.example.com`) or
  `RENDER_EXTERNAL_HOSTNAME`; loopback is always allowed for health checks. Others get `400`.
- **CORS** is off unless explicit origins are configured; `*` is rejected; no credentials.
- **Production mode** disables `/docs`, `/redoc`, and `/openapi.json` and requires a token
  for `/metrics`.
- **Input bounds** on every field and the body; validation errors never echo input.
- **Container:** non-root user (uid 10001), no build tools or tests in the runtime image,
  read-only root filesystem and dropped capabilities in Compose.

## 13. Local development

```
python -m venv .venv
.venv\Scripts\activate                      # Windows; use `source .venv/bin/activate` elsewhere
python -m pip install -e ".[dev]"
copy .env.example .env                      # then set API_KEY_PEPPER in .env
uvicorn gateway.main:app --reload
```

The first start creates `./data/gateway.db`. Generate a pepper with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`, create a key with
`gateway-create-key --client-id dev`, and call the API (see [Example requests](#18-example-requests)).
In development, `/docs` and `/metrics` are available without extra configuration.

## 14. Docker

The image is a two-stage `python:3.12-slim` build. The runtime stage contains only the
virtualenv, runs as the unprivileged `gateway` user, defaults to `APP_ENV=production`, stores
SQLite at `/app/data/gateway.db` (a volume), binds `0.0.0.0:$PORT` (default `8000`) with one
Uvicorn worker, and has a `HEALTHCHECK` on `/health`. `.dockerignore` keeps `.env`, databases,
tests, and VCS data out of the build context.

```
docker build -t llm-api-gateway .

docker run -d --name gateway -p 127.0.0.1:8000:8000 -v gateway-data:/app/data \
  -e API_KEY_PEPPER -e METRICS_TOKEN \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  llm-api-gateway

docker exec gateway gateway-create-key --client-id my-client
docker exec gateway gateway-revoke-key --key-id <key id>
```

(`-e NAME` without a value passes the variable from your shell, so secrets are not typed into
the command.)

**Compose** runs the same image with a named volume, a read-only root filesystem, dropped
capabilities, and the port bound to `127.0.0.1`. Secrets come from `./.env` (or
`GATEWAY_ENV_FILE`); `GATEWAY_PORT` changes the host port.

```
docker compose up -d --build --wait
docker compose exec gateway gateway-create-key --client-id my-client
docker compose down          # keeps the database volume;  `down -v` deletes it
```

With a volume, keys, usage, and cache entries survive restarts; rate-limit counters reset.

## 15. Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_ENV` | `development` (`production` in the image) | `development` enables docs and open `/metrics`; `production` requires the pepper |
| `LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |
| `API_KEY_PEPPER` | — | **Secret.** HMAC key for API-key hashes (32+ chars) |
| `API_KEY_SEEDS` | — | **Secret.** `client_id:key_id:key_hash` entries inserted at startup if missing |
| `METRICS_TOKEN` | — | **Secret.** Bearer token for `/metrics` (32+ chars) |
| `GROQ_API_KEY` | — | **Secret.** Enables `model: "groq"` |
| `GROQ_MODEL_NAME` | `llama-3.3-70b-versatile` | Upstream Groq model |
| `GOOGLE_API_KEY` | — | **Secret.** Enables `model: "gemini"` |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash` | Upstream Gemini model |
| `DATABASE_URL` | `sqlite:///./data/gateway.db` (`sqlite:////app/data/gateway.db` in the image) | SQLite file |
| `CACHE_ENABLED` | `false` | Enable the response cache |
| `CACHE_TTL_SECONDS` | `300` | Cache entry lifetime (≤ 7 days) |
| `CACHE_MAX_ENTRIES` | `1000` | Maximum cached responses |
| `PROVIDER_TIMEOUT_SECONDS` | `30` | Per-attempt provider timeout (≤ 300) |
| `MAX_RETRIES` | `2` | Retries after the first attempt (0–5) |
| `RETRY_BASE_DELAY` | `0.5` | Backoff base, seconds |
| `RETRY_MAX_DELAY` | `4` | Backoff cap, seconds (≥ base) |
| `RATE_LIMIT_REQUESTS` | `60` | Requests per window, per client |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Window / full-refill period |
| `TRUSTED_HOSTS` | `localhost,127.0.0.1` | Allowed `Host` headers (loopback always allowed) |
| `CORS_ORIGINS` | *(empty: off)* | Explicit browser origins |
| `PORT` | `8000` | Container listen port (set by platforms such as Render) |
| `RENDER_EXTERNAL_HOSTNAME` | — | Set by Render; that exact hostname is trusted automatically |
| `APP_NAME`, `APP_VERSION` | `LLM API Gateway`, `0.1.0` | Title and `/health` version |

Invalid values stop startup with an error naming the setting. Empty values count as unset.
Never commit `.env` or put secret values in source, images, or deployment files.

## 16. Deployment

The repository includes a [Render](https://render.com) Blueprint (`render.yaml`) for a
**free** Docker web service. Render was chosen because its free web services need no credit
card, build directly from this repository's `Dockerfile`, provide HTTPS on
`*.onrender.com`, and support secret environment variables. Free instances have 512 MB RAM
(the gateway idles at roughly 60 MB), **no persistent disk, no shell**, and spin down after
15 minutes without traffic (about a minute to wake).

Because of the missing shell and disk, keys reach the deployment through `API_KEY_SEEDS`
(hash-only entries), not the database CLI.

1. **Create secrets locally** (store them in a password manager; never commit or share them):

   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"      # API_KEY_PEPPER
   ```

2. **Create a key and its seed entry** with that pepper in your local environment
   (`API_KEY_PEPPER` in your shell or local `.env`):

   ```
   gateway-create-key --client-id demo --seed
   ```

   Keep the printed raw key private; the `demo:<key id>:<hash>` line is what goes to Render.

3. **Create the service:** Render Dashboard → **New** → **Blueprint** → connect this GitHub
   repository. Render reads `render.yaml` (region Singapore, plan free, health check
   `/ready`, `APP_ENV=production`, `CACHE_ENABLED=true`) and prompts for:

   | Variable | Value |
   |----------|-------|
   | `API_KEY_PEPPER` | the pepper from step 1 |
   | `API_KEY_SEEDS` | the seed line(s) from step 2, comma-separated |

   `METRICS_TOKEN` is generated by Render (read it under the service's **Environment** tab).
   To enable real providers, add `GROQ_API_KEY` and/or `GOOGLE_API_KEY` there afterwards
   (without them, `groq`/`gemini` return `503 provider_not_configured` and `mock` still works).
   `TRUSTED_HOSTS` is not needed: Render's `RENDER_EXTERNAL_HOSTNAME` is trusted
   automatically. Add a custom domain to `TRUSTED_HOSTS` only if you configure one.

4. **Deploy.** The deploy goes live only when `/ready` returns `200`. The URL is
   `https://<service-name>.onrender.com`. Auto-deploy is off (`autoDeployTrigger: off`), so
   later commits are deployed with **Manual Deploy**.

5. **Verify** with the smoke test (secrets are read from the environment and never printed):

   ```
   read -rs GATEWAY_API_KEY && export GATEWAY_API_KEY      # paste the raw key; not echoed
   read -rs METRICS_TOKEN && export METRICS_TOKEN
   python scripts/smoke_test.py https://<service-name>.onrender.com --rate-limit
   python scripts/smoke_test.py https://<service-name>.onrender.com --provider groq   # optional, billable
   ```

   It checks `/health`, `/ready`, disabled docs, `/metrics` policy, security headers,
   missing/invalid/valid (and optionally revoked, via `GATEWAY_REVOKED_API_KEY`) keys, the
   response shape and request ID, cache `MISS` → `HIT`, and — with `--rate-limit` — the `429`
   and `Retry-After`.

6. **Revoke a key** by removing its entry from `API_KEY_SEEDS` and redeploying (the database
   starts empty on every deploy, so the key disappears).

## 17. Production limitations

This is a production-style gateway suitable for learning, portfolio demonstration, and small
single-instance workloads — not enterprise infrastructure.

- **SQLite is single-node.** One writer at a time; fine for one instance, not for a fleet.
- **Free-tier storage is ephemeral.** On Render's free plan the database is lost on every
  spin-down, restart, and deploy: usage history, cache entries, and CLI-created keys vanish.
  Only `API_KEY_SEEDS` keys come back. Durable storage needs a paid persistent disk or a
  database service.
- **Cold starts.** A free Render instance sleeps after 15 idle minutes; the next request
  waits about a minute.
- **Rate limits are per process.** The limiter is in memory, resets on restart, and would be
  multiplied across multiple instances or workers (the image runs one worker).
- **Cache is shared and bounded, not distributed.** It lives in the same SQLite file.
- **Usage retention.** Usage rows are append-only with no retention policy or reporting API.
- **Providers.** Real completions depend on Groq/Gemini availability, quotas, and model
  deprecations; there is no fallback between providers.
- **Worst-case latency** is about `(MAX_RETRIES + 1) × PROVIDER_TIMEOUT_SECONDS`, holding a
  worker thread meanwhile.
- **Logs** from Uvicorn itself (start/stop) are plain text, not JSON.

## 18. Example requests

```
curl -X POST https://<host>/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock", "messages": [{"role": "user", "content": "Explain REST APIs"}], "temperature": 0}'

curl https://<host>/ready
curl -H "Authorization: Bearer $METRICS_TOKEN" https://<host>/metrics
```

PowerShell:

```
$headers = @{ Authorization = "Bearer $env:GATEWAY_API_KEY" }
$body = '{"model": "groq", "messages": [{"role": "user", "content": "Say hello in one sentence."}]}'
Invoke-RestMethod -Method Post -Uri https://<host>/v1/chat/completions -Headers $headers -ContentType "application/json" -Body $body
```

## 19. Example response

```
HTTP/1.1 200 OK
X-Request-ID: req_3f2b9c0e8a1d4e6f9b7c5a2d1e0f4b3c
X-Cache: MISS
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 59
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

```json
{
  "id": "req_3f2b9c0e8a1d4e6f9b7c5a2d1e0f4b3c",
  "object": "chat.completion",
  "model": "mock",
  "provider": "mock",
  "content": "Mock response: Explain REST APIs",
  "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
}
```

The same request again returns `X-Cache: HIT`, the same `content`, a new `id`, and
`"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}`.

## 20. Project structure

```
LLM-API-GATEWAY/
├── src/gateway/
│   ├── main.py                 app factory, startup (database, key seeding, logging), error handlers
│   ├── config.py               settings from environment / .env
│   ├── errors.py               client-safe error types and the error envelope
│   ├── middleware.py           request context, trusted hosts, body size limit
│   ├── rate_limit.py           RateLimiter protocol, in-memory token bucket
│   ├── api/                    routes, schemas, auth / rate-limit / metrics dependencies
│   ├── auth/                   keys, hashing, models, store, service, bootstrap, CLI
│   ├── observability/          request-ID context, JSON logging, Prometheus metrics
│   ├── persistence/            SQLite connections, migrations, repositories
│   ├── providers/              LLMProvider protocol, factory, mock, Groq, Gemini
│   └── services/               inference flow, retries, cache, usage
├── tests/                      pytest suite (offline)
├── scripts/smoke_test.py       deployment smoke test (standard library only)
├── Dockerfile                  two-stage, non-root runtime image
├── compose.yaml                local single-node runtime with a volume
├── render.yaml                 Render Blueprint (no secret values)
├── .dockerignore  .env.example  .gitignore  pyproject.toml  LICENSE
```

## 21. Testing

```
python -m pytest
```

The 477 tests run offline in about 15 seconds and need no configuration: provider SDKs are
replaced with fakes that return real SDK objects, an autouse fixture blocks non-loopback
network access, every test uses its own temporary SQLite database, and time-based behaviour
(retries, rate limits, cache TTL) uses injected clocks and sleeps. They cover key hashing and
lifecycle, every `401` path, validation bounds, retry classification and attempt counts,
concurrent rate limiting, persistence across restarts and concurrent writes, cache and usage
semantics, fail-open/fail-closed paths, request IDs, structured logs, every metric family,
readiness, security headers, trusted hosts, CORS, key seeding, and — in every relevant path —
that keys, hashes, peppers, and prompts never reach responses, logs, metrics, or the
database file.

The Docker image is verified separately (build, non-root user, health check, `/ready`,
`/metrics` policy, inference, cache, retries, rate limiting, persistence, revocation, and no
secrets in logs or metrics), and deployments with `scripts/smoke_test.py`.

## 22. Future improvements

- PostgreSQL for keys and usage, and Redis for rate limits and cache, behind the existing
  `ApiKeyStore`, `UsageRecorder`, `CacheStore`, and `RateLimiter` protocols — enabling
  multiple instances.
- Streaming responses (server-sent events).
- Provider fallback and per-client provider/model allow-lists.
- Per-client quotas and a usage reporting endpoint, plus a usage retention policy.
- An authenticated admin API for key management.
- A lockfile for reproducible image builds, and CI running tests and image scans.
- JSON-formatted Uvicorn lifecycle logs; OpenTelemetry tracing.
