"""Worker side of a run: claim -> running -> AgentRunner -> terminal status.

Pure orchestration over ports, so it is tested with in-memory adapters and a
fake runner. The worker process (playground_worker) owns the claim loop,
concurrency, the reaper and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from playground_core.errors import DomainError, FailedPreconditionError, NotFoundError
from playground_core.events import Event, EventKind
from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import Actor, ControlKind, Lease, Run, RunLimits, RunStatus
from playground_core.ports import (
    AgentHistory,
    AgentRunner,
    AgentStore,
    Clock,
    ControlChannel,
    ConversationRepository,
    EventLog,
    JobQueue,
    RunChanges,
    RunOutcome,
    RunRepository,
    RunRequest,
)
from playground_core.providers import provider_env, resolve_model
from playground_core.services.agents import AgentService
from playground_core.services.conversations import status_event
from playground_core.services.credentials import CredentialService
from playground_core.services.history import record_change

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LeaseTiming:
    ttl: timedelta = timedelta(seconds=30)
    heartbeat: timedelta = timedelta(seconds=10)


class RunExecutor:
    def __init__(
        self,
        *,
        runs: RunRepository,
        conversations: ConversationRepository,
        agents: AgentService,
        store: AgentStore,
        credentials: CredentialService,
        queue: JobQueue,
        events: EventLog,
        control: ControlChannel,
        runner: AgentRunner,
        clock: Clock,
        worker_id: str,
        timing: LeaseTiming | None = None,
        history: AgentHistory | None = None,
    ) -> None:
        self._runs = runs
        self._conversations = conversations
        self._agents = agents
        self._store = store
        self._credentials = credentials
        self._queue = queue
        self._events = events
        self._control = control
        self._runner = runner
        self._clock = clock
        self._worker_id = worker_id
        self._timing = timing or LeaseTiming()
        self._history = history

    async def execute(self, lease: Lease) -> None:
        """Run the leased job and always complete the lease."""
        try:
            await self._execute(lease)
        finally:
            # Shielded so a shutdown cancel still frees the agent for others.
            await asyncio.shield(self._queue.complete(lease))

    async def _execute(self, lease: Lease) -> None:
        run = await self._runs.get(lease.job.run_id)
        if run is None:
            log.warning("job for unknown run %s dropped", lease.job.run_id)
            return
        if lease.recovered and run.status is RunStatus.RUNNING:
            await self.finish(run, RunStatus.INTERRUPTED, "the worker stopped during this run")
            return
        if run.status is not RunStatus.QUEUED:
            return  # cancelled while queued

        cancel = asyncio.Event()
        async with self._control.subscribe(run.id) as messages:
            watcher = asyncio.create_task(self._watch_control(messages, cancel))
            try:
                await self._run(run, lease, cancel)
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

    async def _watch_control(self, messages: Any, cancel: asyncio.Event) -> None:
        async for msg in messages:
            if msg.kind is ControlKind.CANCEL:
                cancel.set()

    async def _run(self, queued: Run, lease: Lease, cancel: asyncio.Event) -> None:
        now = self._clock.now()
        run = await self._runs.transition(
            queued.id,
            {RunStatus.QUEUED},
            RunStatus.RUNNING,
            RunChanges(started_at=now, heartbeat_at=now, worker=self._worker_id),
        )
        if run is None:
            return  # cancelled between the read and the claim
        await self.emit(run, EventKind.RUN_STATUS, status_event(run))

        try:
            request = await self._prepare(run)
        except DomainError as exc:
            await self.finish(run, RunStatus.FAILED, str(exc))
            return

        lost = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(run, lease, cancel, lost))

        async def sink(
            kind: EventKind, data: Mapping[str, Any], *, parent_tool_use_id: str = ""
        ) -> None:
            await self.emit(run, kind, data, parent_tool_use_id=parent_tool_use_id)

        try:
            outcome = await self._runner.run(request, sink, cancel)
        except asyncio.CancelledError:
            # Worker shutdown past its grace period.
            await asyncio.shield(
                self.finish(run, RunStatus.INTERRUPTED, "the worker shut down during this run")
            )
            raise
        except Exception:
            log.exception("runner crashed on run %s", run.id)
            outcome = RunOutcome(session_id="", status=RunStatus.FAILED, error="runner crashed")
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        # Snapshot what the turn changed (memory/, workspace/) while the agent
        # lock is still held, tagged with the run id.
        summary = " ".join(run.prompt.split())[:72]
        await record_change(
            self._history, run.agent_slug, f"Run: {summary}", Actor.agent(run.agent_slug, run.id)
        )
        if outcome.session_id:
            await self._save_session(run.conversation_id, outcome.session_id)
        status, error = outcome.status, outcome.error
        if lost.is_set():
            status, error = RunStatus.INTERRUPTED, "lost the agent lock (worker stalled)"
        await self.finish(run, status, error, outcome)

    async def _prepare(self, run: Run) -> RunRequest:
        report = await self._agents.validate(run.agent_slug)
        if not report.runnable:
            raise FailedPreconditionError("agent has validation errors; fix them before running")
        raw = (await self._store.read(run.agent_slug, MANIFEST_FILE)).data
        manifest = AgentManifest.model_validate_json(raw)
        cred = await self._credentials.resolve(
            cred_id=run.credential_set_id, name=manifest.credential_set
        )
        env = provider_env(cred, self._credentials.reveal(cred))
        model = resolve_model(manifest.model, cred)
        await self._runs.transition(
            run.id, {RunStatus.RUNNING}, RunStatus.RUNNING, RunChanges(credential_set_id=cred.id)
        )
        conv = await self._conversations.get(run.conversation_id)
        if conv is None:
            raise NotFoundError("conversation was deleted")
        return RunRequest(
            run_id=run.id,
            conversation_id=run.conversation_id,
            agent_slug=run.agent_slug,
            prompt=run.prompt,
            resume_session_id=conv.session_id,
            model=model,
            max_turns=manifest.limits.max_turns,
            max_budget_usd=manifest.limits.max_budget_usd,
            env=env,
            limits=RunLimits(
                timeout_seconds=manifest.limits.timeout_seconds,
                memory_mb=manifest.limits.memory_mb,
                cpus=manifest.limits.cpus,
                max_processes=manifest.limits.max_processes,
            ),
            network_allowlist=tuple(manifest.network.allowlist),
        )

    async def _heartbeat(
        self, run: Run, lease: Lease, cancel: asyncio.Event, lost: asyncio.Event
    ) -> None:
        interval = self._timing.heartbeat.total_seconds()
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self._queue.renew(lease, self._timing.ttl):
                    log.error("run %s lost its agent lock; stopping it", run.id)
                    lost.set()
                    cancel.set()
                    return
                await self._runs.heartbeat(run.id, self._clock.now())
            except Exception:
                # Transient infra errors: keep trying until the lease expires.
                log.exception("heartbeat failed for run %s", run.id)

    async def _save_session(self, conversation_id: str, session_id: str) -> None:
        conv = await self._conversations.get(conversation_id)
        if conv is not None and conv.session_id != session_id:
            await self._conversations.update(
                replace(conv, session_id=session_id, updated_at=self._clock.now())
            )

    async def finish(
        self,
        run: Run,
        status: RunStatus,
        error: str = "",
        outcome: RunOutcome | None = None,
    ) -> None:
        done = await self._runs.transition(
            run.id,
            {RunStatus.RUNNING},
            status,
            RunChanges(
                finished_at=self._clock.now(),
                error=error,
                usage=outcome.usage if outcome else None,
            ),
        )
        if done is not None:
            await self.emit(done, EventKind.RUN_STATUS, status_event(done))

    async def emit(
        self,
        run: Run,
        kind: EventKind,
        data: Mapping[str, Any],
        *,
        parent_tool_use_id: str = "",
    ) -> None:
        await self._events.append(
            Event(
                run.conversation_id,
                run.id,
                kind,
                data,
                parent_tool_use_id=parent_tool_use_id,
                time=self._clock.now(),
            )
        )
