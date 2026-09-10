# LLM API Gateway

A backend API gateway that provides a unified interface for interacting with multiple LLM providers.

## Objective

This project provides a unified backend gateway for multiple LLM providers. It focuses on
backend and infrastructure concerns — authentication, reliability, rate limiting, caching,
usage tracking, and observability — rather than on any single AI application. These
capabilities are introduced incrementally, phase by phase.

## Current Status

Phase 1 — Core Inference API + Provider Abstraction

Implemented:

- `POST /v1/chat/completions` with strict request validation
- A provider abstraction with Mock, Groq, and Gemini implementations
- A normalized response format, independent of the provider
- Consistent, machine-readable error responses

Authentication, retries/fallback, rate limiting, caching, persistence, and observability are
**not** implemented yet.

## Architecture

```
Client
 ↓
FastAPI  (routes + Pydantic validation)
 ↓
Inference Service
 ↓
Provider Factory
 ├── Mock
 ├── Groq
 └── Gemini
```

- **API layer** (`gateway/api`) validates requests and delegates to the inference service.
  It contains no provider-specific logic.
- **Inference service** (`gateway/services/inference.py`) resolves a provider, converts the
  API request into an internal `ProviderRequest`, calls the provider, and builds the
  `ChatCompletionResponse` (including a `req_<uuid>` request ID).
- **Providers** (`gateway/providers`) implement the `LLMProvider` protocol:
  `generate(ProviderRequest) -> ProviderResponse`. Each SDK is imported only in its own
  module (`groq.py`, `gemini.py`).
- **Provider factory** (`gateway/providers/factory.py`) maps the requested `model` to a
  provider instance, using configuration for credentials and model names.

## API

### `POST /v1/chat/completions`

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

| Field         | Type   | Rules                                                      |
|---------------|--------|------------------------------------------------------------|
| `model`       | string | Required. One of `mock`, `groq`, `gemini`                  |
| `messages`    | array  | Required, at least one message                             |
| `role`        | string | `system`, `user`, or `assistant`                           |
| `content`     | string | Required, must contain non-whitespace text                 |
| `temperature` | number | Optional, `0.0`–`2.0`                                      |
| `max_tokens`  | int    | Optional, greater than `0`                                 |

Unknown fields are rejected. Streaming is not supported yet.

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
| 422    | `invalid_request`         | Validation failed (includes a `details` list)          |
| 502    | `provider_error`          | The upstream provider call failed or returned no text  |
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

Health endpoint:

```
http://127.0.0.1:8000/health
```

Try a mock completion (no API keys needed):

```
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "mock", "messages": [{"role": "user", "content": "Hello"}]}'
```

Interactive API docs are available at `http://127.0.0.1:8000/docs`.

## Configuration

Settings are read from environment variables, and optionally from a local `.env` file
(which is git-ignored). Copy `.env.example` to `.env` and fill in only what you need.

| Variable            | Default                   | Purpose                                 |
|---------------------|---------------------------|-----------------------------------------|
| `APP_NAME`          | `LLM API Gateway`         | Application title                       |
| `APP_VERSION`       | `0.1.0`                   | Version reported by `/health`           |
| `APP_ENV`           | `development`             | Environment name                        |
| `LOG_LEVEL`         | `INFO`                    | Log level                               |
| `GROQ_API_KEY`      | *(unset)*                 | Groq API key                            |
| `GROQ_MODEL_NAME`   | `llama-3.3-70b-versatile` | Groq model used for `model: "groq"`     |
| `GOOGLE_API_KEY`    | *(unset)*                 | Google AI Studio (Gemini) API key       |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash`        | Gemini model used for `model: "gemini"` |

Provider keys are optional. The application starts, `/health` works, and the mock provider
works without any keys. Requesting `groq` or `gemini` without its key returns a `503`
`provider_not_configured` error. Empty values are treated as unset.

Never commit a real `.env` file or paste keys into source code or documentation.

## Testing

```
python -m pytest -v
```

The automated tests **never call external providers**. The Groq and Gemini SDK clients are
replaced with in-process fakes that return real SDK response objects, and an autouse
fixture blocks any non-loopback network connection, so a test that accidentally reached the
network would fail.

### Manual smoke test against a real provider

Only run this when you have valid credentials locally. It makes a real, billable request.

1. Add the relevant key to your local `.env` (`GROQ_API_KEY` and/or `GOOGLE_API_KEY`).
2. Start the server: `uvicorn gateway.main:app`
3. Send a request with `model` set to `groq` or `gemini`:

   ```
   curl -X POST http://127.0.0.1:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model": "groq", "messages": [{"role": "user", "content": "Say hello in one sentence."}]}'
   ```

   PowerShell:

   ```
   $body = '{"model": "gemini", "messages": [{"role": "user", "content": "Say hello in one sentence."}]}'
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/completions -ContentType "application/json" -Body $body
   ```

4. Expect `200` with `provider` set to the provider you chose and non-null `usage` counts.

## Project Structure

```
LLM-API-GATEWAY/
├── src/
│   └── gateway/
│       ├── config.py              # Settings (env / .env)
│       ├── errors.py              # Client-safe error types
│       ├── main.py                # App creation and error handlers
│       ├── api/
│       │   ├── routes.py          # /health and /v1/chat/completions
│       │   └── schemas.py         # Request/response models
│       ├── services/
│       │   └── inference.py       # Provider-independent inference flow
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

- [x] Phase 0 — Repository and API foundation
- [x] Phase 1 — Core inference API + provider abstraction
- [ ] Phase 2 — *(provider abstraction merged into Phase 1)*
- [ ] Phase 3 — Authentication and validation
- [ ] Phase 4 — Reliability and provider fallback
- [ ] Phase 5 — Rate limiting and quotas
- [ ] Phase 6 — Caching and usage tracking
- [ ] Phase 7 — Observability
- [ ] Phase 8 — Persistence
- [ ] Phase 9 — Docker, testing and security
- [ ] Phase 10 — Deployment and documentation
