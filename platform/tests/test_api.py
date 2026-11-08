"""End-to-end through the real grpc.aio server (in-process, memory drivers)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import grpc
import pytest
from grpc import aio
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr

from playground_api.app import build_container, build_server
from playground_api.config import Settings
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    common_pb2,
    file_pb2,
    file_pb2_grpc,
)

from .conftest import ADMIN_PASSWORD, ADMIN_USER, TEMPLATES


class Client:
    def __init__(self, channel: aio.Channel) -> None:
        self.channel = channel
        self.auth = auth_pb2_grpc.AuthServiceStub(channel)
        self.agents = agent_pb2_grpc.AgentServiceStub(channel)
        self.files = file_pb2_grpc.FileServiceStub(channel)
        self.token = ""

    @property
    def md(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),)

    async def login(self) -> None:
        resp = await self.auth.Login(
            auth_pb2.LoginRequest(username=ADMIN_USER, password=ADMIN_PASSWORD)
        )
        self.token = resp.token


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[Client]:
    settings = Settings(
        agents_root=tmp_path / "agents",
        templates_root=TEMPLATES,
        agent_index_driver="memory",
        session_driver="memory",
        admin_username=ADMIN_USER,
        admin_password=SecretStr(ADMIN_PASSWORD),
    )
    async with AsyncExitStack() as stack:
        server = await build_server(await build_container(settings, stack))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        async with aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield Client(channel)
        await server.stop(grace=None)


async def _code(call: object) -> grpc.StatusCode:
    with pytest.raises(aio.AioRpcError) as info:
        await call  # type: ignore[misc]
    return info.value.code()


async def test_requires_auth(client: Client) -> None:
    assert await _code(client.agents.ListAgents(agent_pb2.ListAgentsRequest())) == (
        grpc.StatusCode.UNAUTHENTICATED
    )
    bad = (("authorization", "Bearer nope"),)
    assert await _code(client.agents.ListAgents(agent_pb2.ListAgentsRequest(), metadata=bad)) == (
        grpc.StatusCode.UNAUTHENTICATED
    )
    assert await _code(
        client.auth.Login(auth_pb2.LoginRequest(username=ADMIN_USER, password="x"))
    ) == (grpc.StatusCode.UNAUTHENTICATED)


async def test_health_is_public(client: Client) -> None:
    stub = health_pb2_grpc.HealthStub(client.channel)  # pyright: ignore[reportUnknownVariableType]
    resp = await stub.Check(health_pb2.HealthCheckRequest(service=""))  # pyright: ignore
    assert resp.status == health_pb2.HealthCheckResponse.SERVING  # pyright: ignore


async def test_login_whoami_logout(client: Client) -> None:
    await client.login()
    me = await client.auth.WhoAmI(auth_pb2.WhoAmIRequest(), metadata=client.md)
    assert me.username == ADMIN_USER
    await client.auth.Logout(auth_pb2.LogoutRequest(), metadata=client.md)
    assert await _code(client.auth.WhoAmI(auth_pb2.WhoAmIRequest(), metadata=client.md)) == (
        grpc.StatusCode.UNAUTHENTICATED
    )


async def test_create_and_edit_agent(client: Client) -> None:
    await client.login()
    md = client.md
    templates = await client.agents.ListTemplates(agent_pb2.ListTemplatesRequest(), metadata=md)
    assert {t.name for t in templates.templates} == {"blank", "assistant"}

    created = await client.agents.CreateAgent(
        agent_pb2.CreateAgentRequest(slug="helper", name="Helper", template="assistant"),
        metadata=md,
    )
    assert created.agent.slug == "helper"
    assert await _code(
        client.agents.CreateAgent(agent_pb2.CreateAgentRequest(slug="helper"), metadata=md)
    ) == (grpc.StatusCode.ALREADY_EXISTS)

    listed = await client.agents.ListAgents(agent_pb2.ListAgentsRequest(), metadata=md)
    assert [a.slug for a in listed.agents] == ["helper"]

    tree = await client.files.GetTree(file_pb2.GetTreeRequest(slug="helper"), metadata=md)
    assert "CLAUDE.md" in {e.path for e in tree.entries}

    chunks = [
        m
        async for m in client.files.ReadFile(
            file_pb2.ReadFileRequest(slug="helper", path="CLAUDE.md"), metadata=md
        )
    ]
    etag = chunks[0].etag
    assert chunks[0].chunk.startswith(b"# Helper")

    wrote = await client.files.WriteFile(
        file_pb2.WriteFileRequest(slug="helper", path="CLAUDE.md", content=b"# New", if_match=etag),
        metadata=md,
    )
    assert wrote.etag != etag
    stale = client.files.WriteFile(
        file_pb2.WriteFileRequest(slug="helper", path="CLAUDE.md", content=b"x", if_match=etag),
        metadata=md,
    )
    assert await _code(stale) == grpc.StatusCode.ABORTED

    bad_manifest = await client.files.WriteFile(
        file_pb2.WriteFileRequest(slug="helper", path="agent.json", content=b"{"), metadata=md
    )
    assert bad_manifest.diagnostics[0].severity == common_pb2.Diagnostic.SEVERITY_ERROR
    report = await client.agents.ValidateAgent(
        agent_pb2.ValidateAgentRequest(slug="helper"), metadata=md
    )
    assert not report.runnable

    await client.agents.DeleteAgent(agent_pb2.DeleteAgentRequest(slug="helper"), metadata=md)
    assert await _code(
        client.agents.GetAgent(agent_pb2.GetAgentRequest(slug="helper"), metadata=md)
    ) == (grpc.StatusCode.NOT_FOUND)


async def test_error_mapping(client: Client) -> None:
    await client.login()
    md = client.md
    await client.agents.CreateAgent(agent_pb2.CreateAgentRequest(slug="a"), metadata=md)
    cases = [
        (
            file_pb2.WriteFileRequest(slug="a", path="../b/x", content=b""),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
        (
            file_pb2.WriteFileRequest(slug="a", path="state/x", content=b""),
            grpc.StatusCode.PERMISSION_DENIED,
        ),
        (
            file_pb2.WriteFileRequest(slug="Nope!", path="x", content=b""),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
        (file_pb2.WriteFileRequest(slug="ghost", path="x", content=b""), grpc.StatusCode.NOT_FOUND),
    ]
    for req, code in cases:
        assert await _code(client.files.WriteFile(req, metadata=md)) == code, req


async def test_large_file_streams_in_chunks(client: Client) -> None:
    await client.login()
    md = client.md
    await client.agents.CreateAgent(agent_pb2.CreateAgentRequest(slug="big"), metadata=md)
    payload = bytes(range(256)) * (10 * 1024)  # 2.5 MiB
    await client.files.WriteFile(
        file_pb2.WriteFileRequest(slug="big", path="workspace/blob.bin", content=payload),
        metadata=md,
    )
    parts = [
        m.chunk
        async for m in client.files.ReadFile(
            file_pb2.ReadFileRequest(slug="big", path="workspace/blob.bin"), metadata=md
        )
    ]
    assert len(parts) == 3 and b"".join(parts) == payload
