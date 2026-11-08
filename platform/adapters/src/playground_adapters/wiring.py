"""Composition root for infrastructure: the only place that picks adapters.

The API and the worker both call ``build_infra`` with their settings, so a
driver switch (for example ``DATABASE_DRIVER=memory``) applies to both.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import (
    InMemoryAgentRepository,
    InMemoryControlChannel,
    InMemoryConversationRepository,
    InMemoryCredentialRepository,
    InMemoryEventLog,
    InMemoryJobQueue,
    InMemoryRunRepository,
    InMemorySessionStore,
)
from playground_adapters.secrets import AesGcmSecretStore, parse_master_key
from playground_adapters.templates import FsTemplateCatalog
from playground_core.clock import SystemClock
from playground_core.ports import (
    AgentRepository,
    AgentStore,
    Clock,
    ControlChannel,
    ConversationRepository,
    CredentialRepository,
    EventLog,
    JobQueue,
    RunRepository,
    SecretStore,
    SessionStore,
    TemplateCatalog,
)

log = logging.getLogger(__name__)


class InfraSettings(BaseSettings):
    """Environment shared by the API and the worker (see deploy/.env.example)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    agents_root: Path = Path("/data/agents")
    templates_root: Path = Path("agents/templates")

    # "memory" keeps everything in-process: tests and quick demos only. Runs
    # need a worker process sharing the same Redis, so use "redis" for them.
    database_driver: Literal["postgres", "memory"] = "postgres"
    redis_driver: Literal["redis", "memory"] = "redis"
    database_url: str = "postgresql://playground:playground@postgres:5432/playground"
    redis_url: str = "redis://redis:6379/0"

    # base64 of 32 random bytes; encrypts credential secrets at rest.
    secrets_key: SecretStr = Field(default=SecretStr(""))

    log_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class Infra:
    clock: Clock
    store: AgentStore
    templates: TemplateCatalog
    agents: AgentRepository
    conversations: ConversationRepository
    runs: RunRepository
    credentials: CredentialRepository
    secrets: SecretStore
    sessions: SessionStore
    queue: JobQueue
    events: EventLog
    control: ControlChannel


async def _postgres(
    settings: InfraSettings, stack: AsyncExitStack
) -> tuple[AgentRepository, ConversationRepository, RunRepository, CredentialRepository]:
    from psycopg_pool import AsyncConnectionPool

    from playground_adapters.postgres import (
        Pool,
        PostgresAgentRepository,
        PostgresConversationRepository,
        PostgresCredentialRepository,
        PostgresRunRepository,
        migrate,
    )

    pool: Pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True, timeout=30)
    stack.push_async_callback(pool.close)
    applied = await migrate(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    return (
        PostgresAgentRepository(pool),
        PostgresConversationRepository(pool),
        PostgresRunRepository(pool),
        PostgresCredentialRepository(pool),
    )


async def _redis(
    settings: InfraSettings, stack: AsyncExitStack
) -> tuple[SessionStore, JobQueue, EventLog, ControlChannel]:
    from redis.asyncio import Redis

    from playground_adapters.redis_runs import RedisControlChannel, RedisEventLog, RedisJobQueue
    from playground_adapters.redis_sessions import RedisSessionStore

    client = Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
    stack.push_async_callback(client.aclose)
    await client.ping()  # pyright: ignore[reportUnknownMemberType]
    return (
        RedisSessionStore(client),
        RedisJobQueue(client),
        RedisEventLog(client),
        RedisControlChannel(client),
    )


async def build_infra(settings: InfraSettings, stack: AsyncExitStack) -> Infra:
    secrets = AesGcmSecretStore(parse_master_key(settings.secrets_key.get_secret_value()))
    if settings.database_driver == "memory":
        repos: tuple[
            AgentRepository, ConversationRepository, RunRepository, CredentialRepository
        ] = (
            InMemoryAgentRepository(),
            InMemoryConversationRepository(),
            InMemoryRunRepository(),
            InMemoryCredentialRepository(),
        )
    else:
        repos = await _postgres(settings, stack)
    if settings.redis_driver == "memory":
        rt: tuple[SessionStore, JobQueue, EventLog, ControlChannel] = (
            InMemorySessionStore(),
            InMemoryJobQueue(),
            InMemoryEventLog(),
            InMemoryControlChannel(),
        )
    else:
        rt = await _redis(settings, stack)
    return Infra(
        clock=SystemClock(),
        store=LocalAgentStore(settings.agents_root),
        templates=FsTemplateCatalog(settings.templates_root),
        agents=repos[0],
        conversations=repos[1],
        runs=repos[2],
        credentials=repos[3],
        secrets=secrets,
        sessions=rt[0],
        queue=rt[1],
        events=rt[2],
        control=rt[3],
    )
