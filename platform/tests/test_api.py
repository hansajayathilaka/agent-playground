"""End-to-end through the real grpc.aio server (in-process, memory drivers)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import grpc
import pytest
from grpc import aio
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr

from playground_adapters.secrets import generate_master_key
from playground_api.app import build_container, build_server
from playground_api.config import Settings
from playground_proto.platform.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    auth_pb2,
    auth_pb2_grpc,
    common_pb2,
    credential_pb2,
    credential_pb2_grpc,
    event_pb2,
    event_pb2_grpc,
    file_pb2,
    file_pb2_grpc,
    run_pb2,
    run_pb2_grpc,
)
from playground_worker.config import WorkerSettings
from playground_worker.main import build_worker

from .conftest import ADMIN_PASSWORD, ADMIN_USER, TEMPLATES
from .runs_harness import FakeRunner, running


class Client:
    def __init__(self, channel: aio.Channel, runner: FakeRunner) -> None:
        self.channel = channel
        self.runner = runner
        self.auth = auth_pb2_grpc.AuthServiceStub(channel)
        self.agents = agent_pb2_grpc.AgentServiceStub(channel)
        self.files = file_pb2_grpc.FileServiceStub(channel)
        self.runs = run_pb2_grpc.RunServiceStub(channel)
        self.conversations = run_pb2_grpc.ConversationServiceStub(channel)
        self.events = event_pb2_grpc.EventServiceStub(channel)
        self.credentials = credential_pb2_grpc.CredentialServiceStub(channel)
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
        database_driver="memory",
        redis_driver="memory",
        secrets_key=SecretStr(generate_master_key()),
        admin_username=ADMIN_USER,
        admin_password=SecretStr(ADMIN_PASSWORD),
        watch_heartbeat_seconds=0.2,
    )
    runner = FakeRunner()
    async with AsyncExitStack() as stack:
        container = await build_container(settings, stack)
        server = await build_server(container)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        # The worker is a separate service in production; here it shares the
        # in-memory infrastructure with the API.
        worker = build_worker(
            container.infra, WorkerSettings(worker_id="w", heartbeat_seconds=1), runner
        )
        async with running(worker), aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield Client(channel, runner)
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


async def _collect(
    client: Client, conversation_id: str, after: int, until: str
) -> list[event_pb2.Event]:
    """Watch until a run_status event with status ``until``."""
    out: list[event_pb2.Event] = []
    call = client.events.WatchEvents(
        event_pb2.WatchEventsRequest(conversation_id=conversation_id, after_offset=after),
        metadata=client.md,
    )
    async with asyncio.timeout(10):
        async for resp in call:
            out.append(resp.event)
            ev = resp.event
            if ev.WhichOneof("body") == "run_status" and ev.run_status.status == until:
                call.cancel()
                break
    return out


async def test_chat_streams_resumes_and_survives_reconnect(client: Client) -> None:
    await client.login()
    md = client.md
    await client.agents.CreateAgent(agent_pb2.CreateAgentRequest(slug="chat"), metadata=md)

    no_creds = client.runs.StartRun(
        run_pb2.StartRunRequest(agent_slug="chat", prompt="hi"), metadata=md
    )
    assert await _code(no_creds) == grpc.StatusCode.FAILED_PRECONDITION

    created = await client.credentials.CreateCredentialSet(
        credential_pb2.CreateCredentialSetRequest(
            input=credential_pb2.CredentialSetInput(
                name="main", provider=credential_pb2.PROVIDER_ANTHROPIC
            ),
            secret="sk-very-secret",
        ),
        metadata=md,
    )
    cs = created.credential_set
    assert cs.is_default and cs.has_secret
    listed = await client.credentials.ListCredentialSets(
        credential_pb2.ListCredentialSetsRequest(), metadata=md
    )
    assert b"sk-very-secret" not in listed.SerializeToString()

    conv = (
        await client.conversations.CreateConversation(
            run_pb2.CreateConversationRequest(agent_slug="chat"), metadata=md
        )
    ).conversation
    watcher = asyncio.create_task(_collect(client, conv.id, 0, "succeeded"))
    await asyncio.sleep(0.1)
    sent = await client.conversations.SendMessage(
        run_pb2.SendMessageRequest(conversation_id=conv.id, text="hello"), metadata=md
    )
    assert sent.run.status == run_pb2.RUN_STATUS_QUEUED
    live = await watcher
    bodies = [e.WhichOneof("body") for e in live if e.offset]
    assert bodies[0] == "user_message" and "assistant_text" in bodies and "text_delta" in bodies
    sub = [e for e in live if e.parent_tool_use_id == "t1"]
    assert sub and sub[0].assistant_text.text == "from subagent"
    assert [e.offset for e in live if e.offset] == list(range(1, len(bodies) + 1))

    # A page refresh: replay everything, or resume after the last offset seen.
    last = max(e.offset for e in live)
    send2 = client.conversations.SendMessage(
        run_pb2.SendMessageRequest(conversation_id=conv.id, text="again"), metadata=md
    )
    second_run = (await send2).run
    resumed = await _collect(client, conv.id, last, "succeeded")
    real = [e for e in resumed if e.offset]
    assert real[0].offset == last + 1 and real[0].user_message.text == "again"
    assert all(e.run_id == second_run.id for e in real)
    assert client.runner.requests[1].resume_session_id == f"session-{conv.id}"

    details = await client.conversations.GetConversation(
        run_pb2.GetConversationRequest(conversation_id=conv.id), metadata=md
    )
    assert [r.status for r in details.runs] == [run_pb2.RUN_STATUS_SUCCEEDED] * 2
    assert details.conversation.title == "hello"
    convs = await client.conversations.ListConversations(
        run_pb2.ListConversationsRequest(agent_slug="chat"), metadata=md
    )
    assert [c.id for c in convs.conversations] == [conv.id]
    runs = await client.runs.ListRuns(run_pb2.ListRunsRequest(agent_slug="chat"), metadata=md)
    assert [r.id for r in runs.runs] == [second_run.id, sent.run.id]

    # Credentials in use by agent.json can't be deleted.
    await client.files.WriteFile(
        file_pb2.WriteFileRequest(
            slug="chat", path="agent.json", content=b'{"name": "chat", "credential_set": "main"}'
        ),
        metadata=md,
    )
    assert await _code(
        client.credentials.DeleteCredentialSet(
            credential_pb2.DeleteCredentialSetRequest(id=cs.id), metadata=md
        )
    ) == (grpc.StatusCode.FAILED_PRECONDITION)


async def test_cancel_and_heartbeats(client: Client) -> None:
    await client.login()
    md = client.md
    await client.agents.CreateAgent(agent_pb2.CreateAgentRequest(slug="slow"), metadata=md)
    await client.credentials.CreateCredentialSet(
        credential_pb2.CreateCredentialSetRequest(
            input=credential_pb2.CredentialSetInput(name="main"), secret="k"
        ),
        metadata=md,
    )
    started = await client.runs.StartRun(
        run_pb2.StartRunRequest(agent_slug="slow", prompt="wait forever"), metadata=md
    )
    run_id, conv_id = started.run.id, started.conversation.id
    await asyncio.wait_for(client.runner.started.wait(), 5)
    watcher = asyncio.create_task(_collect(client, conv_id, 0, "cancelled"))
    await asyncio.sleep(0.5)  # idle long enough for a heartbeat
    await client.runs.CancelRun(run_pb2.CancelRunRequest(run_id=run_id), metadata=md)
    events = await watcher
    assert any(e.WhichOneof("body") == "heartbeat" and e.offset == 0 for e in events)
    got = await client.runs.GetRun(run_pb2.GetRunRequest(run_id=run_id), metadata=md)
    assert got.run.status == run_pb2.RUN_STATUS_CANCELLED
    assert got.run.HasField("finished_at")
    assert await _code(
        client.events.WatchEvents(
            event_pb2.WatchEventsRequest(conversation_id="nope"), metadata=md
        ).read()
    ) == (grpc.StatusCode.NOT_FOUND)
