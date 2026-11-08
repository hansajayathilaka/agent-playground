from __future__ import annotations

from pydantic import Field, SecretStr

from playground_adapters.wiring import InfraSettings


class Settings(InfraSettings):
    """API configuration from environment variables (see deploy/.env.example)."""

    grpc_host: str = "0.0.0.0"  # noqa: S104 - bound inside the container network
    grpc_port: int = 50051

    admin_username: str = "admin"
    admin_password: SecretStr = Field(default=SecretStr(""))
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60)

    # WatchEvents sends a heartbeat after this many idle seconds so proxies
    # and browsers keep the stream open.
    watch_heartbeat_seconds: float = Field(default=15, gt=0)
