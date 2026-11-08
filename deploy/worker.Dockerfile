# syntax=docker/dockerfile:1
# Worker image. Build context: repository root.
# The Claude CLI ships inside the claude-agent-sdk wheel; no Node.js needed.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.11.32 \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY platform/core/pyproject.toml platform/core/
COPY platform/adapters/pyproject.toml platform/adapters/
COPY platform/proto/pyproject.toml platform/proto/
COPY platform/api/pyproject.toml platform/api/
COPY platform/worker/pyproject.toml platform/worker/
RUN uv sync --frozen --no-dev --no-install-workspace --package playground-worker

COPY platform platform
COPY agents/templates agents/templates
RUN uv sync --frozen --no-dev --package playground-worker --no-editable

# Runs as an unprivileged user. Phase 3 adds the bubblewrap sandbox, which
# needs extra privileges for this container only (decision 7).
RUN useradd --system --uid 10001 --home /data/home playground \
 && mkdir -p /data/agents /data/sessions /data/home && chown -R playground /data
USER playground
ENV PATH=/opt/venv/bin:$PATH \
    AGENTS_ROOT=/data/agents \
    SESSIONS_ROOT=/data/sessions \
    TEMPLATES_ROOT=/app/agents/templates
CMD ["playground-worker"]
