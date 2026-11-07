"""Composition root: the only place that picks concrete adapters."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta

from grpc import aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from playground_adapters.auth import StaticAdminAuthProvider
from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import InMemoryAgentRepository, InMemorySessionStore
from playground_adapters.templates import FsTemplateCatalog
from playground_api.config import Settings
from playground_api.guard import GuardInterceptor
from playground_api.servicers import AgentServicer, AuthServicer, FileServicer
from playground_core.clock import SystemClock
from playground_core.ports import AgentRepository, SessionStore
from playground_core.services.agents import AgentService
from playground_core.services.auth import AuthService
from playground_core.services.files import FileService
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    file_pb2,
    file_pb2_grpc,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    agents: AgentService
    files: FileService
    auth: AuthService


async def _agent_repository(settings: Settings, stack: AsyncExitStack) -> AgentRepository:
    if settings.agent_index_driver == "memory":
        return InMemoryAgentRepository()
    from psycopg_pool import AsyncConnectionPool

    from playground_adapters.postgres import Pool, PostgresAgentRepository, migrate

    pool: Pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True, timeout=30)
    stack.push_async_callback(pool.close)
    applied = await migrate(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    return PostgresAgentRepository(pool)


async def _session_store(settings: Settings, stack: AsyncExitStack) -> SessionStore:
    if settings.session_driver == "memory":
        return InMemorySessionStore()
    from redis.asyncio import Redis

    from playground_adapters.redis_sessions import RedisSessionStore

    client = Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
    stack.push_async_callback(client.aclose)
    await client.ping()  # pyright: ignore[reportUnknownMemberType]
    return RedisSessionStore(client)


async def build_container(settings: Settings, stack: AsyncExitStack) -> Container:
    clock = SystemClock()
    store = LocalAgentStore(settings.agents_root)
    repo = await _agent_repository(settings, stack)
    sessions = await _session_store(settings, stack)
    agents = AgentService(store, repo, FsTemplateCatalog(settings.templates_root), clock)
    await agents.reconcile()
    provider = StaticAdminAuthProvider(
        settings.admin_username, settings.admin_password.get_secret_value()
    )
    auth = AuthService(provider, sessions, clock, timedelta(seconds=settings.session_ttl_seconds))
    return Container(agents, FileService(store, agents), auth)


async def build_server(container: Container) -> aio.Server:
    server = aio.server(
        interceptors=[GuardInterceptor(container.auth)],
        options=[
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.keepalive_permit_without_calls", 1),
        ],
    )
    auth_pb2_grpc.add_AuthServiceServicer_to_server(AuthServicer(container.auth), server)
    agent_pb2_grpc.add_AgentServiceServicer_to_server(AgentServicer(container.agents), server)
    file_pb2_grpc.add_FileServiceServicer_to_server(FileServicer(container.files), server)

    names = [
        auth_pb2.DESCRIPTOR.services_by_name["AuthService"].full_name,
        agent_pb2.DESCRIPTOR.services_by_name["AgentService"].full_name,
        file_pb2.DESCRIPTOR.services_by_name["FileService"].full_name,
    ]
    await _add_health(server, names)
    reflection.enable_server_reflection(
        [*names, reflection.SERVICE_NAME, health.SERVICE_NAME], server
    )
    return server


async def _add_health(server: aio.Server, names: list[str]) -> None:
    # grpcio-health-checking ships no type information for its asyncio servicer.
    servicer = health.aio.HealthServicer()  # pyright: ignore
    health_pb2_grpc.add_HealthServicer_to_server(servicer, server)  # pyright: ignore
    for name in [*names, ""]:
        await servicer.set(name, health_pb2.HealthCheckResponse.SERVING)  # pyright: ignore
