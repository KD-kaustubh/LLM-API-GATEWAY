# syntax=docker/dockerfile:1

# ---- build: install the package and its runtime dependencies into a virtualenv ----
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# ---- runtime: only the virtualenv, no build tools, sources, or tests ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    DATABASE_URL=sqlite:////app/data/gateway.db

RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin gateway \
    && mkdir -p /app/data \
    && chown gateway:gateway /app/data

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
USER gateway:gateway

EXPOSE 8000
VOLUME ["/app/data"]

# Liveness only: no API key, no provider, no database access.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# One worker: rate limits are per process and SQLite is the single-node store.
# The gateway writes its own JSON access log, so uvicorn's access log is disabled.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-server-header"]
