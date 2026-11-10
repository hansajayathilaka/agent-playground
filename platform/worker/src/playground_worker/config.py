from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from playground_adapters.wiring import InfraSettings
from playground_worker.jail import SandboxSettings


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

    # Isolation (Phase 3, see jail.py). "none" is for development only.
    sandbox_mode: Literal["bwrap", "none"] = "bwrap"
    # "proxy": no direct network, HTTP(S) through the per-run egress proxy.
    # "host": the jail shares the worker's network (can reach Redis/Postgres).
    sandbox_network: Literal["proxy", "host"] = "proxy"
    # Delegated cgroup v2 subtree for per-run limits (set by the entrypoint).
    run_cgroup_root: Path | None = None
    # Worker-owned dir for per-run egress sockets.
    run_runtime_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "playground-runtime"
    )
    # Defaults for agent.json limits.* when an agent leaves them unset.
    run_timeout_seconds: int = Field(default=3600, ge=10)
    run_memory_mb: int = Field(default=4096, ge=128)
    run_cpus: float = Field(default=2.0, gt=0)
    run_max_processes: int = Field(default=512, ge=16)
    run_max_file_mb: int = Field(default=4096, ge=1)

    def sandbox(self) -> SandboxSettings:
        return SandboxSettings(
            mode=self.sandbox_mode,
            network=self.sandbox_network,
            cgroup_root=self.run_cgroup_root,
            runtime_dir=self.run_runtime_dir,
            timeout_seconds=self.run_timeout_seconds,
            memory_mb=self.run_memory_mb,
            cpus=self.run_cpus,
            max_processes=self.run_max_processes,
            max_file_mb=self.run_max_file_mb,
            builder_slug=self.builder_slug,
        )
