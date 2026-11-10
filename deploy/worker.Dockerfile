# syntax=docker/dockerfile:1
# Worker image. Build context: repository root.
# The Claude CLI ships inside the claude-agent-sdk wheel; no Node.js needed.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.11.32 \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep bubblewrap \
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
COPY agents/_builder agents/_builder
RUN uv sync --frozen --no-dev --package playground-worker --no-editable

# Agents run in a bubblewrap jail (platform/worker/.../jail.py), which needs
# user namespaces and a fresh /proc, so compose runs this container
# privileged (decision 7). The entrypoint starts as root only to delegate a
# cgroup v2 subtree for per-run limits, then drops to the playground user
# with no capabilities; the worker itself never runs as root.
RUN useradd --system --uid 10001 --home /data/home playground \
 && mkdir -p /data/agents /data/sessions /data/home && chown -R playground /data
COPY deploy/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
ENV PATH=/opt/venv/bin:$PATH \
    AGENTS_ROOT=/data/agents \
    SESSIONS_ROOT=/data/sessions \
    TEMPLATES_ROOT=/app/agents/templates \
    BUILDER_SEED_DIR=/app/agents/_builder \
    RUN_RUNTIME_DIR=/run/playground
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
CMD ["playground-worker"]
