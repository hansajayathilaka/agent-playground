FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm
RUN pip install --no-cache-dir uv==0.11.32 \
 && apt-get update \
 && apt-get install -y --no-install-recommends bubblewrap \
 && rm -rf /var/lib/apt/lists/*
