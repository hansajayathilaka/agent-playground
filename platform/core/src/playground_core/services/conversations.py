"""Conversations and runs, API side: create, queue turns, cancel, watch.

The API never runs agents. A message becomes a ``queued`` run row plus a
JobQueue entry; a worker picks it up (see services/execution.py).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from playground_core.errors import FailedPreconditionError, InvalidArgumentError, NotFoundError
from playground_core.events import Event, EventKind
from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import (
    ControlKind,
    ControlMessage,
    Conversation,
    Job,
    Run,
    RunStatus,
)
from playground_core.ports import (
    AgentStore,
    Clock,
    ControlChannel,
    ConversationRepository,
    EventLog,
    JobQueue,
    RunChanges,
    RunRepository,
)
from playground_core.services.agents import AgentService
from playground_core.services.credentials import CredentialService

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 100_000
MAX_TITLE_CHARS = 200


@dataclass(frozen=True, slots=True)
class ConversationDetails:
    conversation: Conversation
    runs: Sequence[Run]


def status_event(run: Run) -> Mapping[str, Any]:
    return {"status": run.status.value, "error": run.error}


class ConversationService:
    def __init__(
        self,
        conversations: ConversationRepository,
        runs: RunRepository,
        agents: AgentService,
        store: AgentStore,
        credentials: CredentialService,
        queue: JobQueue,
        events: EventLog,
        control: ControlChannel,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._runs = runs
        self._agents = agents
        self._store = store
        self._credentials = credentials
        self._queue = queue
        self._events = events
        self._control = control
        self._clock = clock

    # -- conversations ---------------------------------------------------------

    async def create(
        self, agent_slug: str, title: str = "", credential_set_id: str = ""
    ) -> Conversation:
        await self._agents.require(agent_slug)
        if credential_set_id:
            await self._credentials.get(credential_set_id)
        now = self._clock.now()
        conv = Conversation(
            id=str(uuid.uuid4()),
            agent_slug=agent_slug,
            title=title.strip()[:MAX_TITLE_CHARS],
            created_at=now,
            updated_at=now,
            credential_set_id=credential_set_id,
        )
        await self._conversations.add(conv)
        return conv

    async def get(self, conversation_id: str) -> ConversationDetails:
        conv = await self.require(conversation_id)
        runs = await self._runs.list(conversation_id=conversation_id, limit=1000)
        return ConversationDetails(conv, runs)

    async def list(self, agent_slug: str) -> Sequence[Conversation]:
        await self._agents.require(agent_slug)
        return await self._conversations.list(agent_slug)

    async def require(self, conversation_id: str) -> Conversation:
        conv = await self._conversations.get(conversation_id) if conversation_id else None
        if conv is None:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        return conv

    # -- turns -------------------------------------------------------------------

    async def send_message(
        self, conversation_id: str, text: str, credential_set_id: str = ""
    ) -> Run:
        conv = await self.require(conversation_id)
        if not text.strip():
            raise InvalidArgumentError("message is empty")
        if len(text) > MAX_PROMPT_CHARS:
            raise InvalidArgumentError(f"message is longer than {MAX_PROMPT_CHARS} characters")
        await self._check_runnable(conv.agent_slug, credential_set_id or conv.credential_set_id)

        now = self._clock.now()
        run = Run(
            id=str(uuid.uuid4()),
            conversation_id=conv.id,
            agent_slug=conv.agent_slug,
            prompt=text,
            status=RunStatus.QUEUED,
            created_at=now,
            credential_set_id=credential_set_id or conv.credential_set_id,
        )
        await self._runs.add(run)
        title = conv.title or text.strip().splitlines()[0][:80]
        await self._conversations.update(replace(conv, title=title, updated_at=now))
        await self._emit(run, EventKind.USER_MESSAGE, {"text": text})
        await self._emit(run, EventKind.RUN_STATUS, status_event(run))
        try:
            await self._queue.enqueue(Job(run.id, run.agent_slug))
        except Exception:
            log.exception("enqueue failed for run %s", run.id)
            failed = await self._runs.transition(
                run.id,
                {RunStatus.QUEUED},
                RunStatus.FAILED,
                RunChanges(finished_at=self._clock.now(), error="could not queue the run"),
            )
            if failed:
                await self._emit(failed, EventKind.RUN_STATUS, status_event(failed))
            raise
        return run

    async def start_run(
        self, agent_slug: str, prompt: str, credential_set_id: str = ""
    ) -> tuple[Conversation, Run]:
        """A one-off task is a one-turn conversation."""
        # Check first so a rejected task doesn't leave an empty conversation.
        await self._agents.require(agent_slug)
        await self._check_runnable(agent_slug, credential_set_id)
        conv = await self.create(agent_slug, "", credential_set_id)
        run = await self.send_message(conv.id, prompt)
        return conv, run

    async def _check_runnable(self, slug: str, credential_set_id: str) -> None:
        report = await self._agents.validate(slug)
        if not report.runnable:
            raise FailedPreconditionError(
                f"agent {slug!r} has validation errors; fix them before running"
            )
        manifest = AgentManifest.model_validate_json(
            (await self._store.read(slug, MANIFEST_FILE)).data
        )
        await self._credentials.resolve(cred_id=credential_set_id, name=manifest.credential_set)

    # -- runs --------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run:
        run = await self._runs.get(run_id) if run_id else None
        if run is None:
            raise NotFoundError(f"run {run_id!r} not found")
        return run

    async def list_runs(
        self, *, agent_slug: str = "", conversation_id: str = "", limit: int = 100
    ) -> Sequence[Run]:
        if agent_slug:
            await self._agents.require(agent_slug)
        limit = max(1, min(limit or 100, 1000))
        return await self._runs.list(
            agent_slug=agent_slug, conversation_id=conversation_id, limit=limit
        )

    async def queued_runs(self, agent_slug: str) -> Sequence[str]:
        return await self._queue.queued(agent_slug)

    async def cancel(self, run_id: str) -> Run:
        """Idempotent. A queued run is cancelled here; a running one is told
        to stop through the control channel and the worker reports back."""
        run = await self.get_run(run_id)
        if run.status.terminal:
            return run
        cancelled = await self._runs.transition(
            run_id,
            {RunStatus.QUEUED},
            RunStatus.CANCELLED,
            RunChanges(finished_at=self._clock.now(), error="cancelled before it started"),
        )
        if cancelled is not None:
            await self._emit(cancelled, EventKind.RUN_STATUS, status_event(cancelled))
            return cancelled
        await self._control.publish(ControlMessage(run_id, ControlKind.CANCEL))
        return await self.get_run(run_id)

    # -- events --------------------------------------------------------------------

    async def watch(self, conversation_id: str, after: int) -> AsyncIterator[Event]:
        await self.require(conversation_id)
        async for event in self._events.tail(conversation_id, max(0, after)):
            yield event

    async def _emit(self, run: Run, kind: EventKind, data: Mapping[str, Any]) -> None:
        await self._events.append(
            Event(run.conversation_id, run.id, kind, data, time=self._clock.now())
        )
