# LLM API Gateway

A backend API gateway that provides a unified interface for interacting with multiple LLM providers.

## Objective

This project provides a unified backend gateway for multiple LLM providers. It focuses on
backend and infrastructure concerns — authentication, reliability, rate limiting, caching,
usage tracking, and observability — rather than on any single AI application. These
capabilities will be introduced incrementally, phase by phase.

## Current Status

Phase 0 — Project Foundation

## Planned Architecture

```
Client
  ↓
FastAPI Gateway
  ↓
Request Processing
  ↓
Provider Layer
  ↓
LLM Providers
```

Provider routing, authentication, reliability, rate limiting, caching, persistence, and
observability are planned for future phases and are **not** implemented yet.

## Tech Stack

- Python
- FastAPI
- Pydantic
- Uvicorn
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

Run the tests:

```
python -m pytest
```

Health endpoint:

```
http://127.0.0.1:8000/health
```

## Environment Configuration

Copy `.env.example` to `.env` and adjust values as needed. No secrets are required for
Phase 0; the file only configures basic application metadata and logging.

## Project Structure

```
LLM-API-GATEWAY/
├── src/
│   └── gateway/
│       ├── __init__.py
│       ├── config.py
│       ├── main.py
│       └── api/
│           ├── __init__.py
│           ├── routes.py
│           └── schemas.py
├── tests/
│   ├── __init__.py
│   └── test_health.py
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── LICENSE
```

## Roadmap

- Phase 0 — Repository and API foundation
- Phase 1 — Core inference API
- Phase 2 — Provider abstraction
- Phase 3 — Authentication and validation
- Phase 4 — Reliability and provider fallback
- Phase 5 — Rate limiting and quotas
- Phase 6 — Caching and usage tracking
- Phase 7 — Observability
- Phase 8 — Persistence
- Phase 9 — Docker, testing and security
- Phase 10 — Deployment and documentation
