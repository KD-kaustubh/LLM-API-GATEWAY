# LLM API Gateway

A backend API gateway that provides a unified interface for interacting with multiple LLM providers.

## Objective

This project provides a unified backend gateway for multiple LLM providers. It focuses on
backend and infrastructure concerns — authentication, reliability, rate limiting, caching,
usage tracking, and observability — rather than on any single AI application. These
capabilities are introduced incrementally, phase by phase.

## Current Status

Phase 5 — Observability + Docker + Security

Implemented:

- `POST /v1/chat/completions` with strict, bounded request validation
- A provider abstraction with Mock, Groq, and Gemini implementations
- A normalized response format, independent of the provider
- API-key authentication for all `/v1` endpoints (`/health` stays public)
- Hashed API-key storage in SQLite, with CLI key creation and revocation
- Request body size limit and consistent, machine-readable error responses
- Provider timeouts and bounded retries with backoff for transient provider failures
- Per-client rate limiting (`429` with `Retry-After`)
- Per-request usage records (tokens, latency, cache hit) in SQLite
- Opt-in response cache for deterministic requests, with TTL and a size cap
- Structured JSON logs with per-request IDs, Prometheus metrics, and `/ready`
- Security headers, trusted-host checks, opt-in CORS, and a non-root Docker image

Provider fallback, quotas, deployment, and distributed (multi-instance) state are **not**
implemented yet.

## Architecture

```
Client
 ↓
FastAPI
 ├── Request context (middleware)          request ID, security headers, access log, metrics
 ├── Trusted host (middleware)             → 400
 ├── CORS (middleware, only if configured)
 ├── Body size limit (middleware)          → 413
 ├── API-key authentication (dependency)   → 401
 ├── Rate limit per client (dependency)    → 429
 └── Pydantic request validation           → 422
 ↓
Inference Service
 ├── Provider Factory (once per request)   → 400 / 503
 ├── Cache lookup (temperature 0 only)     → HIT: return cached answer, skip provider
 ├── Retrier (transient errors only)       → 502 when exhausted
 │    ↓
 │   Provider (with per-attempt timeout)
 │    ├── Mock
 │    ├── Groq
 │    └── Gemini
 ├── Cache store (successful answers only)
 └── Usage record (every completed request)

SQLite (gateway/persistence)
 ├── api_keys        key metadata and hashes
 ├── usage_records   one row per completed request
 └── cache_entries   cached responses with expiry
```

- **API layer** (`gateway/api`) validates requests and delegates to the inference service.
  It contains no provider-specific logic.
- **Authentication** (`gateway/auth`) generates, hashes, stores, and verifies API keys. It
  knows nothing about providers or inference; `gateway/api/security.py` adapts it to FastAPI.
- **Inference service** (`gateway/services/inference.py`) resolves a provider, converts the
  API request into an internal `ProviderRequest`, calls the provider through the `Retrier`
  (`gateway/services/retry.py`), and builds the `ChatCompletionResponse` (including a
  `req_<uuid>` request ID). It does not know how API keys are stored.
- **Rate limiter** (`gateway/rate_limit.py`) implements the `RateLimiter` protocol; the
  `enforce_rate_limit` dependency applies it to every `/v1` route after authentication.
- **Cache and usage** (`gateway/services/cache.py`, `gateway/services/usage.py`) define the
  `CacheStore` and `UsageRecorder` protocols used by the inference service.
- **Persistence** (`gateway/persistence`) holds all SQL: connection handling, schema
  migrations, and SQLite implementations of `ApiKeyStore`, `UsageRecorder`, and `CacheStore`.
  The API layer and services never touch SQLite directly.
- **Observability** (`gateway/observability`) holds the request-ID context, the JSON log
  formatter, and the Prometheus metric definitions; `gateway/middleware.py` applies them to
  every request.
- **Providers** (`gateway/providers`) implement the `LLMProvider` protocol:
  `generate(ProviderRequest) -> ProviderResponse`. Each SDK is imported only in its own
  module (`groq.py`, `gemini.py`).
- **Provider factory** (`gateway/providers/factory.py`) maps the requested `model` to a
  provider instance, using configuration for credentials and model names.

## Authentication

Every request to a `/v1` endpoint must include a gateway API key as a Bearer token:

```
Authorization: Bearer gw_live_...
```

Keys look like `gw_live_<key id>_<secret>`: a 16-character hex key ID (not secret, used to
look up the credential) followed by a 43-character URL-safe secret (256 bits of randomness).

| Endpoint                     | Access    |
|------------------------------|-----------|
| `GET /health`                | Public    |
| `POST /v1/chat/completions`  | Protected |

Any authentication failure — missing header, wrong scheme, malformed key, unknown key, or
revoked key — returns the same response, so callers cannot tell which check failed:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer

{"error": {"type": "authentication_error", "message": "Invalid API key"}}
```

### Managing keys

Keys are managed from the command line only; there are no key-management HTTP endpoints.
Both commands use the database configured by `DATABASE_URL`.

1. Set a pepper in your local `.env` (at least 32 characters, random):

   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   ```
   API_KEY_PEPPER=<paste the value>
   ```

2. Create a key:

   ```
   gateway-create-key --client-id my-dev-client
   ```

   The key is hashed and its metadata (key ID, hash, client ID, timestamp) is written to the
   database. The raw key is printed **once** and is never stored; save it somewhere safe. A
   client can have several keys.

3. Use it as `Authorization: Bearer <key>`. Keys persist across server restarts; the running
   server reads them from the database on each request, so no restart is needed.

4. Revoke a key by its key ID (printed at creation, and the 16 hex characters after
   `gw_live_`). Revocation takes effect immediately and persists:

   ```
   gateway-revoke-key --key-id <key id>
   ```

Changing `API_KEY_PEPPER` invalidates every stored key hash.

### Security design

- **Raw keys are never stored.** A key is hashed immediately after generation; the store
  holds only the key ID, the hash, the client ID, a creation timestamp, and an optional
  revocation timestamp. The raw key is returned exactly once, at creation.
- **Cryptographically secure randomness.** Keys are generated with Python's `secrets` module.
- **Keyed hashing.** Hashes are HMAC-SHA256 with a server-side pepper (`API_KEY_PEPPER`).
  Because keys have 256 bits of entropy, brute force is infeasible, and without the pepper a
  leaked hash cannot even be checked offline. (Slow password hashes such as bcrypt exist for
  low-entropy human passwords and would only add latency here.)
- **Timing-safe verification.** Hashes are compared with `hmac.compare_digest`, and an
  unknown key ID still performs a comparison against a dummy hash, so the lookup path does not
  reveal whether a key ID exists.
- **Generic errors.** Clients always get the same `401` body and never learn why a key failed.
- **No secrets in logs or errors.** Failed authentications are logged with the non-secret key
  ID and a reason only. Validation errors return field locations, messages, and error types —
  never submitted values, headers, or credentials — and are capped at 10 entries.
- **Development defaults are safe.** With no pepper configured, the app starts with a random
  per-process pepper (and logs a warning), so no stored key can be verified.
  `APP_ENV=production` refuses to start without a pepper.
- **Persistent, replaceable store.** Key metadata lives in SQLite (`api_keys`). The pepper
  stays in the environment and is never written to the database. Routes depend only on the
  `ApiKeyStore` protocol, so a PostgreSQL store can replace `SQLiteApiKeyStore` later.
- **Fails closed.** If the credential store cannot be read during a request, the request is
  rejected with a generic `500`; it is never let through.

## Reliability

### Timeouts

Every provider attempt has a timeout of `PROVIDER_TIMEOUT_SECONDS` (default 30s), set through
each SDK's own timeout option (Groq: `timeout` in seconds; Gemini: `HttpOptions.timeout` in
milliseconds). Both SDKs' built-in retries are disabled so the gateway is the only retry layer
and the total number of upstream calls stays bounded.

### Retry policy

Retrying a chat completion can duplicate work and cost money, so only failures explicitly
classified as **transient** are retried. Everything else fails on the first attempt.

| Retried (`TransientProviderError`)                  | Not retried                                            |
|-----------------------------------------------------|--------------------------------------------------------|
| Provider timeout (`ProviderTimeoutError`)           | Validation errors (`422`) and authentication (`401`)   |
| Connection / network failures                       | Unsupported model (`400`), missing credentials (`503`) |
| Upstream status `408`, `429`, `502`, `503`, `504`   | Other upstream statuses, including `400`–`404` and `500` |
|                                                     | Empty provider responses, unexpected exceptions        |

Upstream `500` is not retried because it can be deterministic for a given request; only
statuses that signal a temporary condition are.

- Attempts are bounded: at most `MAX_RETRIES + 1` provider calls per request (default 3).
- Backoff is exponential with "equal jitter": before retry *n* the gateway waits between
  50% and 100% of `min(RETRY_MAX_DELAY, RETRY_BASE_DELAY × 2^(n-1))` (defaults: 0.5s, 4s).
- Provider resolution (unknown model, missing key) happens once, outside the retry loop.
- When every attempt fails, the client receives the normal `502 provider_error` with a
  gateway-generated message (for example `Groq request timed out`); SDK exception text is
  never returned or logged.
- Worst-case latency is roughly `(MAX_RETRIES + 1) × PROVIDER_TIMEOUT_SECONDS` plus backoff.
  The request's worker thread is held while it waits. Fallback to a *different* provider is
  not implemented.

## Rate Limiting

Every `/v1` request is rate limited **after** authentication, per authenticated `client_id`.
All keys belonging to one client share its limit; the raw API key is never used as an
identifier, stored, or logged. `/health` is never rate limited, and requests that fail
authentication never touch any client's limit.

- **Algorithm:** token bucket. Each client can burst up to `RATE_LIMIT_REQUESTS`, refilling
  continuously at `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS` (default 60/60s). A
  client idle for a full window is back to its full allowance.
- **Order:** the limit is checked before body validation, so invalid requests also count.
- **Headers:** successful `/v1` responses include `X-RateLimit-Limit` and
  `X-RateLimit-Remaining` for the calling client only.
- **When exceeded:**

  ```
  HTTP/1.1 429 Too Many Requests
  Retry-After: 20
  X-RateLimit-Limit: 3
  X-RateLimit-Remaining: 0

  {"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
  ```

  `Retry-After` is the whole number of seconds until the client's next request is allowed.

- **Limitations:** state is in memory and per process. Limits reset on restart, and running
  several workers or instances multiplies the effective limit. The `RateLimiter` protocol
  lets a shared backend (for example Redis) replace `InMemoryRateLimiter` in a later phase
  without changing routes. Memory stays bounded: only authenticated clients get a bucket,
  and buckets idle for a full window are pruned.

## Persistence

State that must survive restarts is stored in SQLite through the standard-library `sqlite3`
module (no ORM, no extra dependency).

- **Location:** `DATABASE_URL`, default `sqlite:///./data/gateway.db` — relative to the
  directory the server is started from. Use `sqlite:////absolute/path/gateway.db` for an
  absolute path. The parent directory is created on startup. `data/` and all `*.db*` files are
  git-ignored.
- **Initialization:** runs at application startup (not at import). A `schema_migrations`
  table records applied versions; missing migrations are applied in one transaction, existing
  tables and rows are never dropped, and repeated starts are no-ops. A database with a newer
  schema than the code supports is refused.
- **Connections:** one short-lived connection per operation (no connection shared across
  threads), WAL journal mode, a 5-second busy timeout, a transaction per operation, and
  parameterized SQL only.

| Table            | Contents                                                                        |
|------------------|---------------------------------------------------------------------------------|
| `api_keys`       | `key_id` (PK), `key_hash`, `client_id` (indexed), `created_at`, `revoked_at`    |
| `usage_records`  | `request_id` (unique), `client_id`, `key_id`, `model`, `provider`, `created_at`, token counts, `latency_ms`, `cache_hit` — indexed on `(client_id, created_at)` and `created_at` |
| `cache_entries`  | `cache_key` (PK, SHA-256), `response_payload` (JSON), `model`, `created_at`, `expires_at` (indexed), `size_bytes` |

### Failure behaviour

| Failure                         | Behaviour                                                        |
|---------------------------------|------------------------------------------------------------------|
| Database cannot be initialized  | **Fail closed:** the server does not start                       |
| Credential lookup fails         | **Fail closed:** request rejected with a generic `500`           |
| Cache read fails                | **Fail open:** logged, treated as a miss, provider is called     |
| Cache write fails               | **Fail open:** logged, the successful response is returned       |
| Usage write fails               | **Fail open:** logged, the successful response is returned       |

Logged persistence failures include only the exception type, never SQL values, paths, or
secrets. A usage-write failure means that request is missing from `usage_records`; this is a
deliberate trade-off so a bookkeeping problem never turns a successful LLM answer into an error.

## Usage Tracking

Every **successful** completion writes one row to `usage_records`:

- request ID (same as the response `id`), client ID, key ID, timestamp (UTC)
- model and provider, token counts, total latency in milliseconds, and `cache_hit`
- Token counts come from the provider's normalized usage; if the provider did not report a
  value it is stored as `NULL`, never estimated. Mock provider counts are its documented word
  counts.
- Cache hits are recorded with `provider = "cache"`, `cache_hit = 1`, and **zero** tokens,
  because no provider call was made.
- Requests rejected before inference (`401`, `413`, `422`, `429`) and failed completions
  (`400`, `502`, `503`) are not recorded as usage.

The table is shaped for later metrics (request counts, cache hit rate, latency, tokens per
client) but no metrics endpoint exists yet. Usage rows are append-only; there is no retention
policy yet.

## Response Cache

The cache is **off by default** (`CACHE_ENABLED=false`). When enabled, it only ever stores
successful responses to requests that are safe to replay:

- **Eligible:** `temperature` is explicitly `0`. Omitted temperature uses the provider's
  default (non-zero), so those requests are never cached. Streaming does not exist, so it
  cannot be cached.
- **Never cached:** provider errors, timeouts, exhausted retries, empty responses, and any
  request rejected by authentication, rate limiting, or validation. A retried request is
  written to the cache once, after it finally succeeds.
- **Key:** SHA-256 of a canonical JSON document (sorted keys) containing the key-format
  version, provider, configured upstream model, requested `model`, every message (role and
  content, in order), `temperature`, and `max_tokens`. Changing `GROQ_MODEL_NAME` therefore
  never serves answers from the previous model. The key never includes the API key, headers,
  client identity, or request ID.
- **Sharing:** identical requests from **different clients share** a cache entry. This is
  safe today because providers are stateless with respect to the caller; it must be revisited
  if responses ever depend on per-client context.
- **Limits:** entries expire after `CACHE_TTL_SECONDS` (default 300) and expired rows are
  deleted on every write. The table holds at most `CACHE_MAX_ENTRIES` rows (default 1000);
  beyond that the entries closest to expiry are evicted. Responses larger than 64 KiB are not
  cached, so the cache stays under about 64 MB at the defaults.
- **Hit behaviour:** the provider is not called. The response has the normal schema with a new
  `id`, the original `model`, `provider`, and `content`, and `usage` of zero tokens. Every
  `/v1/chat/completions` response includes `X-Cache: HIT`, `MISS` (eligible, not found), or
  `BYPASS` (cache disabled or request not eligible).
- **Rate limiting still applies:** the cache is checked after authentication and rate
  limiting, so a cache hit consumes a rate-limit token like any other request.
- **Stored payload:** model, provider, content, and the original token usage as JSON — never
  credentials, headers, or error details. It is read back with `json.loads`, never
  unpickled or evaluated.

### Future backends

SQLite suits a single instance. The `ApiKeyStore`, `UsageRecorder`, `CacheStore`, and
`RateLimiter` protocols let PostgreSQL (keys, usage) and Redis (cache, rate limits) replace
the current implementations for multi-instance deployments without changing routes.

## API

### `POST /v1/chat/completions`

Requires `Authorization: Bearer <api key>`.

Request:

```json
{
  "model": "mock",
  "messages": [
    {"role": "system", "content": "You are concise."},
    {"role": "user", "content": "Explain REST APIs in simple terms"}
  ],
  "temperature": 0.7,
  "max_tokens": 256
}
```

| Field         | Type   | Rules                                                          |
|---------------|--------|----------------------------------------------------------------|
| `model`       | string | Required, 1–100 chars. One of `mock`, `groq`, `gemini`         |
| `messages`    | array  | Required, 1–100 messages                                       |
| `role`        | string | `system`, `user`, or `assistant`                               |
| `content`     | string | Required, 1–100,000 chars, must contain non-whitespace text    |
| `temperature` | number | Optional, `0.0`–`2.0`                                          |
| `max_tokens`  | int    | Optional, `1`–`32768`                                          |

Unknown fields are rejected, and request bodies larger than 1 MiB are rejected with `413`.
Streaming is not supported yet. Responses carry `X-Cache` (see [Response Cache](#response-cache))
and the rate-limit headers.

Response:

```json
{
  "id": "req_3f2b9c0e8a1d4e6f9b7c5a2d1e0f4b3c",
  "object": "chat.completion",
  "model": "mock",
  "provider": "mock",
  "content": "Mock response: Explain REST APIs in simple terms",
  "usage": {
    "input_tokens": 9,
    "output_tokens": 8,
    "total_tokens": 17
  }
}
```

### Model / provider convention

The `model` field selects a provider. The concrete upstream model is set in configuration,
so clients use a stable name and never need to know provider-specific model IDs.

| `model`  | Provider | Upstream model                          | Credentials       |
|----------|----------|-----------------------------------------|-------------------|
| `mock`   | Mock     | —                                       | None              |
| `groq`   | Groq     | `GROQ_MODEL_NAME`                       | `GROQ_API_KEY`    |
| `gemini` | Gemini   | `GEMINI_MODEL_NAME`                     | `GOOGLE_API_KEY`  |

The response `model` field reports the upstream model that produced the answer (for
example `llama-3.3-70b-versatile`), and `provider` reports which provider was used.

### Token usage

- **Groq / Gemini:** counts are taken directly from the provider's response. If a provider
  does not report a value, that field is `null` — it is never estimated.
- **Mock:** counts are deterministic whitespace word counts. They are **not** real tokens and
  exist only so tests and local development have stable numbers.

### Errors

All errors share one shape:

```json
{"error": {"type": "unsupported_model", "message": "Unsupported model 'gpt-4'. Supported: gemini, groq, mock"}}
```

| Status | `type`                    | When                                                   |
|--------|---------------------------|--------------------------------------------------------|
| 400    | `unsupported_model`       | `model` is not a known provider                        |
| 400    | `invalid_host`            | `Host` header not in `TRUSTED_HOSTS`                   |
| 401    | `authentication_error`    | Missing, malformed, unknown, or revoked API key        |
| 404    | `not_found`               | Unknown route                                          |
| 413    | `request_too_large`       | Request body exceeds 1 MiB                             |
| 422    | `invalid_request`         | Validation failed (includes a bounded `details` list)  |
| 429    | `rate_limit_error`        | Client exceeded its rate limit (see `Retry-After`)     |
| 502    | `provider_error`          | Provider failed, timed out, or returned no text (after any retries) |
| 503    | `provider_not_configured` | The selected provider's API key is not set             |
| 500    | `internal_error`          | Unexpected error (no internal details are exposed)     |

Every response, including errors, carries an `X-Request-ID` header; quote it when reporting a
problem so the matching log lines can be found. The error body itself is unchanged.

## Observability

### Request IDs

Every request gets a server-generated `req_<32 hex>` ID. It is returned in the `X-Request-ID`
header, used as the chat completion `id`, stored as the usage record's `request_id`, and added
to every log line written while the request is handled. A client-supplied `X-Request-ID` is
**ignored**: accepting it would let callers forge or collide IDs (usage request IDs are unique)
and inject arbitrary text into logs.

### Structured logs

Gateway logs are JSON lines on stdout (level from `LOG_LEVEL`), configured at startup:

```json
{"timestamp": "2026-09-10T10:02:10.522+00:00", "level": "INFO", "logger": "gateway.access", "message": "request completed", "request_id": "req_62ee…", "method": "POST", "route": "/v1/chat/completions", "status_code": 200, "latency_ms": 12.4, "client_id": "team-a", "key_id": "1f0e…", "provider": "mock", "model": "mock", "cache_status": "HIT"}
```

- One access line per request (`gateway.access`): method, **route template** (never the raw
  path or query), status, latency, and — when known — client ID, key ID, provider, model, and
  cache status. Other lines cover retries, rate-limit rejections, auth failures (reason and key
  ID), and cache/usage persistence failures.
- The formatter emits only an allow-list of fields. API keys, key hashes, `Authorization`
  headers, peppers, provider keys, prompts, and responses are never logged; tests send secrets
  and prompt canaries through every path and assert they never appear.
- Unhandled exceptions are logged as `exc_type` plus `file:line:function` frames only — never
  the exception message, which could contain request data.
- Uvicorn's own start/stop lines are plain text; its access log is disabled in the Docker image
  because the gateway writes its own.

### Metrics

`GET /metrics` serves Prometheus text format from a dedicated registry:

| Metric | Labels |
|--------|--------|
| `gateway_http_requests_total` | `method`, `route`, `status` |
| `gateway_http_request_duration_seconds` (histogram) | `method`, `route` |
| `gateway_auth_failures_total` | `reason` (`missing_credentials`, `malformed_key`, `unknown_key`, `hash_mismatch`, `revoked_key`, `metrics_token`) |
| `gateway_rate_limit_rejections_total` | — |
| `gateway_inference_requests_total` | `outcome` (`success` or the error type) |
| `gateway_cache_lookups_total` | `result` (`hit`, `miss`, `bypass`) |
| `gateway_completions_total` | `provider` (`cache` for cache hits) |
| `gateway_tokens_total` | `provider`, `direction` (`input`, `output`; provider-reported only) |
| `gateway_provider_calls_total` | `provider`, `outcome` (per attempt) |
| `gateway_provider_errors_total` | `provider`, `error_type` (`timeout`, `transient_error`, `provider_error`, `internal_error`) |
| `gateway_provider_call_duration_seconds` (histogram) | `provider` |
| `gateway_provider_retries_total` | `provider` |

Plus standard `process_*` and `python_gc_*` metrics. Labels only ever take values from fixed
sets: unmatched paths are reported as `route="unmatched"`, unknown HTTP methods as `OTHER`, and
there are no labels for request IDs, client or key IDs, API keys, prompts, or model names.

**Access policy:** metrics reveal traffic and error patterns, so they are not public in
production. With `METRICS_TOKEN` set, `/metrics` requires `Authorization: Bearer
<METRICS_TOKEN>` (compared in constant time; a gateway API key does not work). Without a token,
`/metrics` is open only when `APP_ENV=development` and returns `404` otherwise.

```
curl -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics
```

### Health and readiness

| Endpoint  | Purpose   | Checks | Status |
|-----------|-----------|--------|--------|
| `GET /health` | Liveness | None — no database, no providers | Always `200` while the process serves HTTP |
| `GET /ready`  | Readiness | Startup finished; SQLite file opens read-write and the schema is migrated (one small query, 1s timeout, never creates a file); `API_KEY_PEPPER` is set so keys can be verified | `200` when all pass, otherwise `503` |

```json
{"status": "ready", "checks": {"startup": "ok", "database": "ok", "authentication": "ok"}}
```

Both are public and contain no internal details. Neither calls an LLM provider: a provider
outage makes completions return `502` but leaves the gateway live and ready.

## Security Hardening

- **Response headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` (API
  responses are per-caller and must not be cached by intermediaries). HSTS is left to the TLS
  terminator, which belongs to deployment.
- **Trusted hosts:** requests whose `Host` is not in `TRUSTED_HOSTS` (exact names or
  `*.example.com`) get `400 invalid_host`. Loopback names are always accepted so local and
  container health checks work; they cannot be abused for DNS rebinding.
- **CORS** is off unless `CORS_ORIGINS` lists explicit `http(s)` origins; `*` is rejected at
  startup. Credentials are never allowed (API keys travel in the `Authorization` header).
- **Production surface:** outside `APP_ENV=development`, `/docs`, `/redoc`, and
  `/openapi.json` are disabled and `/metrics` requires a token.
- **Errors:** unhandled exceptions become a generic `500` (with `X-Request-ID`); stack traces,
  paths, SQL, and exception messages never reach clients. The existing body, message, model,
  and `max_tokens` limits are unchanged.
- **Configuration:** all secrets (`API_KEY_PEPPER`, `METRICS_TOKEN`, provider keys) come only
  from the environment; `METRICS_TOKEN` must be at least 32 characters.

## Docker

The image is a two-stage build on `python:3.12-slim`: dependencies are installed into a
virtualenv in the build stage, and the runtime stage contains only that virtualenv. It runs as
the unprivileged `gateway` user (uid 10001), with `APP_ENV=production`, the database at
`/app/data/gateway.db` (a volume), one Uvicorn worker, and a `HEALTHCHECK` that requests
`/health` on loopback. No secrets, tests, `.env`, or databases are copied into the image
(`.dockerignore`).

Build and run:

```
docker build -t llm-api-gateway .

docker run -d --name gateway -p 127.0.0.1:8000:8000 \
  -v gateway-data:/app/data \
  -e API_KEY_PEPPER="$API_KEY_PEPPER" -e METRICS_TOKEN="$METRICS_TOKEN" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  llm-api-gateway
```

Production mode refuses to start without `API_KEY_PEPPER`. Manage keys with the CLI inside
the container (it uses the same volume and environment):

```
docker exec gateway gateway-create-key --client-id my-client
docker exec gateway gateway-revoke-key --key-id <key id>
```

### Docker Compose

`compose.yaml` runs the same image with a named volume (`gateway-data`), a read-only root
filesystem, all capabilities dropped, and the port published on `127.0.0.1` only. Secrets come
from `./.env` (or the file named by `GATEWAY_ENV_FILE`); `GATEWAY_PORT` changes the host port.

```
docker compose up -d --build --wait
docker compose exec gateway gateway-create-key --client-id my-client
curl http://127.0.0.1:8000/ready
docker compose down        # keeps the database volume
docker compose down -v     # also deletes it
```

SQLite data lives in the volume, so API keys, usage, and cache entries survive container
restarts and `down`/`up`. Rate-limit counters are in memory and reset on restart.

## Tech Stack

- Python
- FastAPI
- Pydantic / pydantic-settings
- Uvicorn
- Groq Python SDK (`groq`)
- Google Gen AI SDK (`google-genai`)
- SQLite (standard-library `sqlite3`)
- `prometheus-client` (metrics)
- Docker
- Pytest

Authentication, caching, persistence, and JSON logging use only the Python standard library.

## Local Setup

Create and activate a virtual environment:

```
python -m venv .venv
```

Windows activation:

```
.venv\Scripts\activate
```

Install the package with development dependencies:

```
python -m pip install -e ".[dev]"
```

Run the application:

```
uvicorn gateway.main:app --reload
```

Health, readiness, and (in development) metrics:

```
http://127.0.0.1:8000/health
http://127.0.0.1:8000/ready
http://127.0.0.1:8000/metrics
```

The first start creates `./data/gateway.db`.

Try a mock completion (after creating a key as described in [Managing keys](#managing-keys)):

```
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock", "messages": [{"role": "user", "content": "Hello"}]}'
```

In development, interactive API docs are available at `http://127.0.0.1:8000/docs` (use
**Authorize** to set the Bearer key); they are disabled in production.

## Configuration

Settings are read from environment variables, and optionally from a local `.env` file
(which is git-ignored). Copy `.env.example` to `.env` and fill in only what you need.

| Variable            | Default                   | Purpose                                         |
|---------------------|---------------------------|-------------------------------------------------|
| `APP_NAME`          | `LLM API Gateway`         | Application title                               |
| `APP_VERSION`       | `0.1.0`                   | Version reported by `/health`                   |
| `APP_ENV`           | `development`             | `development` enables docs and open `/metrics`; `production` requires the pepper |
| `LOG_LEVEL`         | `INFO`                    | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `TRUSTED_HOSTS`     | `localhost,127.0.0.1`     | Allowed `Host` headers (loopback always allowed) |
| `CORS_ORIGINS`      | *(empty: CORS off)*       | Explicit browser origins, comma-separated        |
| `METRICS_TOKEN`     | *(unset)*                 | Bearer token for `/metrics` (32+ characters)     |
| `GROQ_API_KEY`      | *(unset)*                 | Groq API key                                    |
| `GROQ_MODEL_NAME`   | `llama-3.3-70b-versatile` | Groq model used for `model: "groq"`             |
| `GOOGLE_API_KEY`    | *(unset)*                 | Google AI Studio (Gemini) API key               |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash`        | Gemini model used for `model: "gemini"`         |
| `API_KEY_PEPPER`    | *(unset)*                 | Secret HMAC key for hashing gateway API keys    |
| `DATABASE_URL`      | `sqlite:///./data/gateway.db` | SQLite file for keys, usage, and cache      |
| `CACHE_ENABLED`     | `false`                   | Enable the response cache                       |
| `CACHE_TTL_SECONDS` | `300`                     | Cache entry lifetime (`0` < value ≤ 7 days)     |
| `CACHE_MAX_ENTRIES` | `1000`                    | Maximum cached responses (`1`–`100000`)         |
| `PROVIDER_TIMEOUT_SECONDS` | `30`               | Per-attempt provider timeout (`0` < value ≤ `300`) |
| `MAX_RETRIES`       | `2`                       | Retries after the first attempt (`0`–`5`)       |
| `RETRY_BASE_DELAY`  | `0.5`                     | Backoff base in seconds (`0`–`30`)              |
| `RETRY_MAX_DELAY`   | `4`                       | Backoff cap in seconds (≥ base, ≤ `60`)         |
| `RATE_LIMIT_REQUESTS` | `60`                    | Requests allowed per window, per client (`1`–`100000`) |
| `RATE_LIMIT_WINDOW_SECONDS` | `60`              | Window / full-refill period in seconds          |

Out-of-range or non-numeric values stop the application at startup with a validation error
naming the setting.

Provider keys are optional. The application starts and `/health` works without any keys.
Requesting `groq` or `gemini` without its key returns a `503` `provider_not_configured`
error. Empty values are treated as unset.

Never commit a real `.env` file or paste keys, peppers, or hashes into source code or
documentation.

## Testing

```
python -m pytest -v
```

The automated tests **never call external providers** and need no configuration:

- Every test that needs a gateway key generates one at runtime with a random pepper; no real
  or real-looking credentials are stored in the test source.
- Security tests cover key format and uniqueness, the `secrets` entropy source, hashing and
  timing-safe verification, the store lifecycle (create/validate/revoke), every `401` path,
  that raw keys never appear in responses, error messages, stored records, or logs, bounded
  and non-echoing validation errors, and the body size limit.
- Reliability tests count provider attempts for every retry scenario (success, transient
  then success, exhaustion, timeouts, and each non-retryable error) using a fake `sleep`, so
  no test waits for real backoff.
- Rate-limit tests use an injected fake clock to advance time deterministically, and include
  a 100-thread concurrency test proving the limit cannot be exceeded by racing requests.
- Persistence tests use a fresh temporary SQLite file per test (and a session-wide temporary
  `DATABASE_URL` as a safety net), so they never touch `./data/gateway.db`. They cover schema
  and indexes, idempotent and concurrent initialization, corrupt databases, parameterized
  SQL, concurrent writes, key persistence across restarts, and raw keys and peppers never
  reaching the database file.
- Cache and usage tests prove that cache hits skip the provider, that failures are never
  cached, the TTL and size limits, that cache hits still consume rate-limit tokens, and that
  cache and usage failures fail open. End-to-end tests boot the real app (startup included)
  against a temporary database.
- Observability tests check request IDs on every response (and that client-supplied ones are
  ignored), structured access-log fields, that logs never contain keys, hashes, `Bearer`
  headers, or prompt text, every metric family, bounded labels, the `/metrics` access policy,
  and that the metrics output contains no secrets or identifiers.
- Health and hardening tests cover `/health` vs `/ready` (before startup, missing pepper, lost
  or deleted database, provider outage), security headers, trusted hosts, CORS, production
  docs/metrics lockdown, and generic `500` responses.

The Docker image is verified manually (it is not part of `pytest`): build, non-root user,
health check, `/ready`, `/metrics` policy, authenticated inference, cache, retries, rate
limiting, persistence across restarts, revocation, and the absence of secrets in logs and
metrics.
- The Groq and Gemini SDK clients are replaced with in-process fakes that return real SDK
  response objects, and an autouse fixture blocks any non-loopback network connection, so a
  test that accidentally reached the network would fail.

### Manual smoke test against a real provider

Only run this when you have valid credentials locally. It makes a real, billable request.

1. Add the relevant provider key to your local `.env` (`GROQ_API_KEY` and/or `GOOGLE_API_KEY`)
   and create a gateway key as described above.
2. Start the server: `uvicorn gateway.main:app`
3. Send a request with `model` set to `groq` or `gemini`:

   ```
   curl -X POST http://127.0.0.1:8000/v1/chat/completions \
     -H "Authorization: Bearer $GATEWAY_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model": "groq", "messages": [{"role": "user", "content": "Say hello in one sentence."}]}'
   ```

   PowerShell:

   ```
   $headers = @{ Authorization = "Bearer $env:GATEWAY_API_KEY" }
   $body = '{"model": "gemini", "messages": [{"role": "user", "content": "Say hello in one sentence."}]}'
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/completions -Headers $headers -ContentType "application/json" -Body $body
   ```

4. Expect `200` with `provider` set to the provider you chose and non-null `usage` counts.

## Project Structure

```
LLM-API-GATEWAY/
├── src/
│   └── gateway/
│       ├── config.py              # Settings (env / .env)
│       ├── errors.py              # Client-safe error types and error envelope
│       ├── main.py                # App creation, startup (database init), error handlers
│       ├── middleware.py          # Request context, trusted hosts, body size limit
│       ├── rate_limit.py          # RateLimiter protocol and in-memory token bucket
│       ├── api/
│       │   ├── routes.py          # /health, /ready, /metrics, /v1/chat/completions
│       │   ├── schemas.py         # Request/response models and validation limits
│       │   └── security.py        # Auth, rate-limit, and metrics-token dependencies
│       ├── observability/
│       │   ├── context.py         # Request ID generation and context variable
│       │   ├── log_config.py      # JSON log formatter and logging setup
│       │   └── metrics.py         # Prometheus registry and metric definitions
│       ├── auth/
│       │   ├── keys.py            # API key generation and parsing
│       │   ├── hashing.py         # HMAC-SHA256 hashing and timing-safe verification
│       │   ├── models.py          # ApiKeyRecord, IssuedApiKey, AuthenticatedClient
│       │   ├── store.py           # ApiKeyStore protocol and in-memory store
│       │   ├── service.py         # Create, revoke, and authenticate keys
│       │   ├── bootstrap.py       # Build the API key service from settings
│       │   └── cli.py             # gateway-create-key / gateway-revoke-key
│       ├── persistence/
│       │   ├── database.py        # DATABASE_URL parsing and per-operation connections
│       │   ├── migrations.py      # Versioned, idempotent schema migrations
│       │   └── repositories.py    # SQLite key store, usage repository, cache store
│       ├── services/
│       │   ├── inference.py       # Inference flow: cache, provider, retries, usage
│       │   ├── cache.py           # Cache key, eligibility, fail-open ResponseCache
│       │   ├── usage.py           # UsageRecord and UsageRecorder protocol
│       │   └── retry.py           # RetryPolicy (backoff) and Retrier
│       └── providers/
│           ├── base.py            # LLMProvider protocol and internal types
│           ├── factory.py         # model -> provider resolution
│           ├── mock.py            # Deterministic offline provider
│           ├── groq.py            # Groq SDK integration
│           └── gemini.py          # Google Gen AI SDK integration
├── tests/
├── Dockerfile
├── compose.yaml
├── .dockerignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── LICENSE
```

## Roadmap

- [x] Phase 0 — Foundation
- [x] Phase 1 — Core API + Provider Abstraction
- [x] Phase 2 — Authentication + Validation
- [x] Phase 3 — Reliability + Rate Limiting
- [x] Phase 4 — Caching + Usage + Persistence
- [x] Phase 5 — Observability + Docker + Security
- [ ] Phase 6 — Deployment + Documentation
