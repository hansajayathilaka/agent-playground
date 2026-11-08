from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration from environment variables (see deploy/.env.example)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    grpc_host: str = "0.0.0.0"  # noqa: S104 - bound inside the container network
    grpc_port: int = 50051

    agents_root: Path = Path("/data/agents")
    templates_root: Path = Path("agents/templates")

    # Adapter selection (composition root). "memory" is for tests and quick demos.
    agent_index_driver: Literal["postgres", "memory"] = "postgres"
    session_driver: Literal["redis", "memory"] = "redis"
    database_url: str = "postgresql://playground:playground@postgres:5432/playground"
    redis_url: str = "redis://redis:6379/0"

    admin_username: str = "admin"
    admin_password: SecretStr = Field(default=SecretStr(""))
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60)

    log_level: str = "INFO"
