from __future__ import annotations

import os
import socket
from pathlib import Path

from pydantic import Field

from playground_adapters.wiring import InfraSettings


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(InfraSettings):
    worker_id: str = Field(default_factory=_default_worker_id)
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    # Per-agent SDK config dirs (session transcripts) live here, outside the
    # agent folders, so any worker can resume any conversation.
    sessions_root: Path = Path("/data/sessions")
    lease_ttl_seconds: float = Field(default=30, ge=5)
    heartbeat_seconds: float = Field(default=10, ge=1)
    # A running run whose heartbeat is older than this is marked interrupted.
    stale_after_seconds: float = Field(default=90, ge=10)
    reaper_interval_seconds: float = Field(default=15, ge=1)
    shutdown_grace_seconds: float = Field(default=30, ge=0)
    # Non-secret variables copied from the worker's env into every run (proxy
    # and CA settings). Everything else in a run's env is built from scratch.
    run_env_passthrough: list[str] = Field(
        default_factory=lambda: [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "NODE_EXTRA_CA_CERTS",
            "TZ",
        ]
    )
