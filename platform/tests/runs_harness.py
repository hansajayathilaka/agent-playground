"""In-memory platform + fake AgentRunner for run/worker tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.git_history import GitAgentHistory
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
from playground_adapters.secrets import AesGcmSecretStore
from playground_adapters.templates import FsTemplateCatalog
from playground_adapters.wiring import Infra
from playground_core.clock import SystemClock
from playground_core.events import Event, EventKind
from playground_core.models import RunStatus
from playground_core.ports import EventSink, RunOutcome, RunRequest
from playground_core.services.agents import AgentService
from playground_core.services.conversations import ConversationService
from playground_core.services.credentials import CredentialInput, CredentialService
from playground_worker.config import WorkerSettings
from playground_worker.main import build_worker
from playground_worker.worker import Worker

from .conftest import TEMPLATES


@dataclass
class FakeRunner:
    """Echoes the prompt. Prompts starting with "wait" block until cancelled
    or released; "boom" raises; "fail" returns FAILED."""

    requests: list[RunRequest] = field(default_factory=list[RunRequest])
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    active: int = 0
    max_active: int = 0

    async def run(self, request: RunRequest, emit: EventSink, cancel: asyncio.Event) -> RunOutcome:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            session = request.resume_session_id or f"session-{request.conversation_id}"
            await emit(EventKind.TEXT_DELTA, {"message_id": "m1", "index": 0, "text": "ec"})
            if request.prompt.startswith("wait"):
                self.started.set()
                stop = asyncio.create_task(cancel.wait())
                go = asyncio.create_task(self.release.wait())
                await asyncio.wait({stop, go}, return_when=asyncio.FIRST_COMPLETED)
                stop.cancel()
                go.cancel()
                if cancel.is_set():
                    return RunOutcome(session, RunStatus.CANCELLED, "cancelled")
            if request.prompt == "boom":
                raise RuntimeError("kaboom")
            if request.prompt == "fail":
                return RunOutcome(session, RunStatus.FAILED, "model said no")
            await emit(
                EventKind.ASSISTANT_TEXT,
                {"message_id": "m1", "index": 0, "text": f"echo: {request.prompt}"},
            )
            await emit(
                EventKind.TOOL_CALL,
                {"tool_use_id": "t1", "name": "Task", "input_json": "{}"},
            )
            await emit(
                EventKind.ASSISTANT_TEXT,
                {"message_id": "m2", "index": 0, "text": "from subagent"},
                parent_tool_use_id="t1",
            )
            return RunOutcome(session, RunStatus.SUCCEEDED)
        finally:
            self.active -= 1


@dataclass
class Platform:
    infra: Infra
    agents: AgentService
    credentials: CredentialService
    conversations: ConversationService
    runner: FakeRunner
    worker: Worker

    async def events(self, conversation_id: str) -> list[Event]:
        return list(await self.infra.events.read(conversation_id, 0, 10_000))

    async def wait_status(
        self, run_id: str, want: set[RunStatus], timeout: float = 5.0
    ) -> RunStatus:
        async with asyncio.timeout(timeout):
            while True:
                run = await self.conversations.get_run(run_id)
                if run.status in want:
                    return run.status
                await asyncio.sleep(0.01)


def memory_infra(agents_root: Path) -> Infra:
    return Infra(
        clock=SystemClock(),
        store=LocalAgentStore(agents_root),
        history=GitAgentHistory(agents_root),
        templates=FsTemplateCatalog(TEMPLATES),
        agents=InMemoryAgentRepository(),
        conversations=InMemoryConversationRepository(),
        runs=InMemoryRunRepository(),
        credentials=InMemoryCredentialRepository(),
        secrets=AesGcmSecretStore(b"k" * 32),
        sessions=InMemorySessionStore(),
        queue=InMemoryJobQueue(),
        events=InMemoryEventLog(),
        control=InMemoryControlChannel(),
    )


def make_platform(infra: Infra, runner: FakeRunner, concurrency: int = 2) -> Platform:
    agents = AgentService(infra.store, infra.agents, infra.templates, infra.clock, infra.history)
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
    settings = WorkerSettings(
        worker_id="test-worker",
        worker_concurrency=concurrency,
        heartbeat_seconds=1,
        lease_ttl_seconds=5,
    )
    worker = build_worker(infra, settings, runner)
    return Platform(infra, agents, credentials, conversations, runner, worker)


@asynccontextmanager
async def running(worker: Worker) -> AsyncGenerator[None]:
    task = asyncio.create_task(worker.run(grace=1))
    try:
        yield
    finally:
        worker.stop()
        await asyncio.wait_for(task, 10)


async def add_default_credentials(credentials: CredentialService) -> str:
    cred = await credentials.create(
        CredentialInput(name="default", default_model="claude-test"),
        "sk-test-secret",
        make_default=True,
    )
    return cred.id
