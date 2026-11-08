"""API composition: builds core services on the shared infrastructure and
registers the gRPC servicers. Adapters are picked in playground_adapters.wiring."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta

from grpc import aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from playground_adapters.auth import StaticAdminAuthProvider
from playground_adapters.wiring import Infra, build_infra
from playground_api.config import Settings
from playground_api.guard import GuardInterceptor
from playground_api.servicers import (
    AgentServicer,
    AuthServicer,
    ConversationServicer,
    CredentialServicer,
    EventServicer,
    FileServicer,
    RunServicer,
)
from playground_core.services.agents import AgentService
from playground_core.services.auth import AuthService
from playground_core.services.conversations import ConversationService
from playground_core.services.credentials import CredentialService
from playground_core.services.files import FileService
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    credential_pb2,
    credential_pb2_grpc,
    event_pb2,
    event_pb2_grpc,
    file_pb2,
    file_pb2_grpc,
    run_pb2,
    run_pb2_grpc,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    infra: Infra
    agents: AgentService
    files: FileService
    auth: AuthService
    credentials: CredentialService
    conversations: ConversationService
    watch_heartbeat_seconds: float = 15.0


def build_services(infra: Infra, settings: Settings) -> Container:
    agents = AgentService(infra.store, infra.agents, infra.templates, infra.clock)
    provider = StaticAdminAuthProvider(
        settings.admin_username, settings.admin_password.get_secret_value()
    )
    auth = AuthService(
        provider, infra.sessions, infra.clock, timedelta(seconds=settings.session_ttl_seconds)
    )
    credentials = CredentialService(infra.credentials, infra.secrets, infra.store, infra.clock)
    conversations = ConversationService(
        infra.conversations,
        infra.runs,
        agents,
        infra.store,
        credentials,
        infra.queue,
        infra.events,
        infra.control,
        infra.clock,
    )
    return Container(
        infra,
        agents,
        FileService(infra.store, agents),
        auth,
        credentials,
        conversations,
        settings.watch_heartbeat_seconds,
    )


async def build_container(settings: Settings, stack: AsyncExitStack) -> Container:
    container = build_services(await build_infra(settings, stack), settings)
    await container.agents.reconcile()
    return container


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
    run_pb2_grpc.add_RunServiceServicer_to_server(RunServicer(container.conversations), server)
    run_pb2_grpc.add_ConversationServiceServicer_to_server(
        ConversationServicer(container.conversations), server
    )
    event_pb2_grpc.add_EventServiceServicer_to_server(
        EventServicer(container.conversations, container.watch_heartbeat_seconds), server
    )
    credential_pb2_grpc.add_CredentialServiceServicer_to_server(
        CredentialServicer(container.credentials), server
    )

    names = [
        auth_pb2.DESCRIPTOR.services_by_name["AuthService"].full_name,
        agent_pb2.DESCRIPTOR.services_by_name["AgentService"].full_name,
        file_pb2.DESCRIPTOR.services_by_name["FileService"].full_name,
        run_pb2.DESCRIPTOR.services_by_name["RunService"].full_name,
        run_pb2.DESCRIPTOR.services_by_name["ConversationService"].full_name,
        event_pb2.DESCRIPTOR.services_by_name["EventService"].full_name,
        credential_pb2.DESCRIPTOR.services_by_name["CredentialService"].full_name,
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
