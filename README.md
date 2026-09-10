# LLM API Gateway

A backend API gateway that provides a unified interface for interacting with multiple LLM providers.

## Objective

This project provides a unified backend gateway for multiple LLM providers. It focuses on
backend and infrastructure concerns — authentication, reliability, rate limiting, caching,
usage tracking, and observability — rather than on any single AI application. These
capabilities are introduced incrementally, phase by phase.

## Current Status

Phase 3 — Reliability + Rate Limiting

Implemented:

- `POST /v1/chat/completions` with strict, bounded request validation
- A provider abstraction with Mock, Groq, and Gemini implementations
- A normalized response format, independent of the provider
- API-key authentication for all `/v1` endpoints (`/health` stays public)
- Hashed credential storage (in-memory for now), key generation, and revocation
- Request body size limit and consistent, machine-readable error responses
- Provider timeouts and bounded retries with backoff for transient provider failures
- Per-client rate limiting (`429` with `Retry-After`)

Provider fallback, quotas, caching, persistence, and observability are **not** implemented yet.

## Architecture

```
Client
 ↓
FastAPI
 ├── Body size limit (middleware)          → 413
 ├── API-key authentication (dependency)   → 401
 ├── Rate limit per client (dependency)    → 429
 └── Pydantic request validation           → 422
 ↓
Inference Service
 ├── Provider Factory (once per request)   → 400 / 503
 └── Retrier (transient errors only)       → 502 when exhausted
      ↓
     Provider (with per-attempt timeout)
      ├── Mock
      ├── Groq
      └── Gemini
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

### Creating a key for local development

There is no database or key-management endpoint yet, so local keys are bootstrapped through
environment variables that hold only **hashes**, never raw keys.

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

   (or `python -m gateway.auth.cli --client-id my-dev-client`)

   The raw key is printed **once**; save it somewhere safe. The command also prints a
   `client_id:key_id:key_hash` entry.

3. Add the entry to your local `.env` (comma-separate multiple entries):

   ```
   API_KEY_HASHES=my-dev-client:<key id>:<key hash>
   ```

4. Restart the server and send the raw key as `Authorization: Bearer <key>`.

Changing `API_KEY_PEPPER` invalidates every existing key hash.

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
- **Development defaults are safe.** With no pepper configured, the app starts with an empty
  credential store and a random per-process pepper. `APP_ENV=production` refuses to start
  without a pepper.
- **The credential store is in-memory and is NOT production persistence.** Keys created at
  runtime disappear on restart. Persistent storage is planned for Phase 4; the `ApiKeyStore`
  protocol lets a database-backed store replace `InMemoryApiKeyStore` without changing routes.

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
Streaming is not supported yet.

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
| 401    | `authentication_error`    | Missing, malformed, unknown, or revoked API key        |
| 404    | `not_found`               | Unknown route                                          |
| 413    | `request_too_large`       | Request body exceeds 1 MiB                             |
| 422    | `invalid_request`         | Validation failed (includes a bounded `details` list)  |
| 429    | `rate_limit_error`        | Client exceeded its rate limit (see `Retry-After`)     |
| 502    | `provider_error`          | Provider failed, timed out, or returned no text (after any retries) |
| 503    | `provider_not_configured` | The selected provider's API key is not set             |
| 500    | `internal_error`          | Unexpected error (no internal details are exposed)     |

## Tech Stack

- Python
- FastAPI
- Pydantic / pydantic-settings
- Uvicorn
- Groq Python SDK (`groq`)
- Google Gen AI SDK (`google-genai`)
- Pytest

Authentication uses only the Python standard library (`secrets`, `hmac`, `hashlib`).

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

Health endpoint (public):

```
http://127.0.0.1:8000/health
```

Try a mock completion (after creating a key as described in
[Creating a key for local development](#creating-a-key-for-local-development)):

```
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock", "messages": [{"role": "user", "content": "Hello"}]}'
```

Interactive API docs are available at `http://127.0.0.1:8000/docs` (use **Authorize** to set
the Bearer key).

## Configuration

Settings are read from environment variables, and optionally from a local `.env` file
(which is git-ignored). Copy `.env.example` to `.env` and fill in only what you need.

| Variable            | Default                   | Purpose                                         |
|---------------------|---------------------------|-------------------------------------------------|
| `APP_NAME`          | `LLM API Gateway`         | Application title                               |
| `APP_VERSION`       | `0.1.0`                   | Version reported by `/health`                   |
| `APP_ENV`           | `development`             | Environment name (`production` requires pepper) |
| `LOG_LEVEL`         | `INFO`                    | Log level                                       |
| `GROQ_API_KEY`      | *(unset)*                 | Groq API key                                    |
| `GROQ_MODEL_NAME`   | `llama-3.3-70b-versatile` | Groq model used for `model: "groq"`             |
| `GOOGLE_API_KEY`    | *(unset)*                 | Google AI Studio (Gemini) API key               |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash`        | Gemini model used for `model: "gemini"`         |
| `API_KEY_PEPPER`    | *(unset)*                 | Secret HMAC key for hashing gateway API keys    |
| `API_KEY_HASHES`    | *(unset)*                 | `client_id:key_id:key_hash` entries, comma-separated |
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
│       ├── main.py                # App creation and error handlers
│       ├── middleware.py          # Request body size limit
│       ├── rate_limit.py          # RateLimiter protocol and in-memory token bucket
│       ├── api/
│       │   ├── routes.py          # /health (public) and /v1/chat/completions (protected)
│       │   ├── schemas.py         # Request/response models and validation limits
│       │   └── security.py        # Authentication and rate-limit dependencies
│       ├── auth/
│       │   ├── keys.py            # API key generation and parsing
│       │   ├── hashing.py         # HMAC-SHA256 hashing and timing-safe verification
│       │   ├── models.py          # ApiKeyRecord, IssuedApiKey, AuthenticatedClient
│       │   ├── store.py           # ApiKeyStore protocol and in-memory store
│       │   ├── service.py         # Create, revoke, and authenticate keys
│       │   ├── bootstrap.py       # Build the credential store from settings
│       │   └── cli.py             # gateway-create-key development helper
│       ├── services/
│       │   ├── inference.py       # Provider-independent inference flow
│       │   └── retry.py           # RetryPolicy (backoff) and Retrier
│       └── providers/
│           ├── base.py            # LLMProvider protocol and internal types
│           ├── factory.py         # model -> provider resolution
│           ├── mock.py            # Deterministic offline provider
│           ├── groq.py            # Groq SDK integration
│           └── gemini.py          # Google Gen AI SDK integration
├── tests/
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
- [ ] Phase 4 — Caching + Usage + Persistence
- [ ] Phase 5 — Observability + Docker + Security
- [ ] Phase 6 — Deployment + Documentation
