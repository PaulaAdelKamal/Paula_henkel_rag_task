# syntax=docker/dockerfile:1
# Grounded RAG chatbot over the iOS 7.1 iPhone User Guide.
#   docker build -t chatbot:1.0 .
#   docker run -p 8000:8000 --env-file .env chatbot:1.0     ->  http://localhost:8000
# Only runtime code and dependencies go in: no ingestion, evaluation, tests or dev tools.

# Python 3.14.10 on Debian trixie and uv 0.13.6, pinned by digest for reproducible builds.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- builder: runtime deps only
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --no-default-groups drops the dev and ingest groups: only [project.dependencies] is installed.
# The optional `ca_bundle` secret is only for building behind a TLS-inspecting proxy
# (see README); a normal build ignores it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-default-groups --no-install-project

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="iphone-guide-rag" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=${GIT_SHA} \
    APP_ENV=prod-demo \
    APP_PORT=8000 \
    CACHE_DIR=/tmp/rag-cache \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app .streamlit/config.toml .streamlit/config.toml
COPY --chown=app:app app/ app/
COPY --chown=app:app data/source/iPhone_User_Guide_1.pdf data/source/iPhone_User_Guide_1.pdf
USER app
EXPOSE 8000
# No curl in the slim image: probe Streamlit's health endpoint with Python.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"APP_PORT\", \"8000\")}/_stcore/health', timeout=4).status == 200 else 1)"]
# Shell form so APP_PORT from --env-file is honoured; exec keeps Streamlit as PID 1 for signals.
CMD ["sh", "-c", "exec python -m streamlit run app/ui.py --server.address 0.0.0.0 --server.port ${APP_PORT:-8000}"]
