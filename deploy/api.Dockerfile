# syntax=docker/dockerfile:1
# API image. Build context: repository root.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
# Dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
COPY platform/core/pyproject.toml platform/core/
COPY platform/adapters/pyproject.toml platform/adapters/
COPY platform/proto/pyproject.toml platform/proto/
COPY platform/api/pyproject.toml platform/api/
RUN uv sync --frozen --no-dev --no-install-workspace --package playground-api

COPY platform platform
COPY agents/templates agents/templates
RUN uv sync --frozen --no-dev --package playground-api --no-editable

RUN useradd --system --uid 10001 --home /app playground \
 && mkdir -p /data/agents && chown -R playground /data
USER playground
ENV PATH=/opt/venv/bin:$PATH \
    AGENTS_ROOT=/data/agents \
    TEMPLATES_ROOT=/app/agents/templates
EXPOSE 50051
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 50051), 2)"
CMD ["playground-api"]
