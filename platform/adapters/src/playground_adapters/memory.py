"""In-memory adapters for tests and single-process development."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.events import Event
from playground_core.models import (
    AgentRecord,
    ControlMessage,
    Conversation,
    CredentialSet,
    Job,
    Lease,
    Principal,
    Run,
    RunStatus,
)
from playground_core.ports import RunChanges


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self._rows: dict[str, AgentRecord] = {}

    async def add(self, record: AgentRecord) -> None:
        if record.slug in self._rows:
            raise AlreadyExistsError(f"agent {record.slug!r} already exists")
        self._rows[record.slug] = record

    async def get(self, slug: str) -> AgentRecord | None:
        return self._rows.get(slug)

    async def list(self) -> Sequence[AgentRecord]:
        return [self._rows[k] for k in sorted(self._rows)]

    async def update(self, record: AgentRecord) -> None:
        if record.slug not in self._rows:
            raise NotFoundError(f"agent {record.slug!r} not found")
        self._rows[record.slug] = record

    async def remove(self, slug: str) -> None:
        self._rows.pop(slug, None)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InMemorySessionStore:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[Principal, float]] = {}

    async def put(self, token: str, principal: Principal, ttl: timedelta) -> None:
        self._rows[hash_token(token)] = (principal, time.monotonic() + ttl.total_seconds())

    async def get(self, token: str) -> Principal | None:
        key = hash_token(token)
        row = self._rows.get(key)
        if row is None:
            return None
        principal, expires = row
        if time.monotonic() >= expires:
            del self._rows[key]
            return None
        return principal

    async def delete(self, token: str) -> None:
        self._rows.pop(hash_token(token), None)


# -- Phase 2 ---------------------------------------------------------------------


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._rows: dict[str, Conversation] = {}

    async def add(self, conversation: Conversation) -> None:
        if conversation.id in self._rows:
            raise AlreadyExistsError(f"conversation {conversation.id!r} already exists")
        self._rows[conversation.id] = conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        return self._rows.get(conversation_id)

    async def list(self, agent_slug: str) -> Sequence[Conversation]:
        rows = [c for c in self._rows.values() if c.agent_slug == agent_slug]
        return sorted(rows, key=lambda c: (c.updated_at, c.id), reverse=True)

    async def update(self, conversation: Conversation) -> None:
        if conversation.id not in self._rows:
            raise NotFoundError(f"conversation {conversation.id!r} not found")
        self._rows[conversation.id] = conversation


def apply_changes(run: Run, to: RunStatus, changes: RunChanges | None) -> Run:
    c = changes or RunChanges()
    updates: dict[str, object] = {"status": to}
    for name in (
        "started_at",
        "finished_at",
        "heartbeat_at",
        "worker",
        "error",
        "credential_set_id",
        "usage",
    ):
        value = getattr(c, name)
        if value is not None:
            updates[name] = value
    return replace(run, **updates)  # pyright: ignore[reportArgumentType]


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._rows: dict[str, Run] = {}
        self._order: list[str] = []

    async def add(self, run: Run) -> None:
        if run.id in self._rows:
            raise AlreadyExistsError(f"run {run.id!r} already exists")
        self._rows[run.id] = run
        self._order.append(run.id)

    async def get(self, run_id: str) -> Run | None:
        return self._rows.get(run_id)

    async def list(
        self, *, conversation_id: str = "", agent_slug: str = "", limit: int = 100
    ) -> Sequence[Run]:
        rows = [self._rows[i] for i in self._order]
        if conversation_id:
            rows = [r for r in rows if r.conversation_id == conversation_id]
        if agent_slug:
            rows = [r for r in rows if r.agent_slug == agent_slug]
        if conversation_id:
            return rows[:limit]
        return list(reversed(rows))[:limit]

    async def transition(
        self,
        run_id: str,
        expected: Collection[RunStatus],
        to: RunStatus,
        changes: RunChanges | None = None,
    ) -> Run | None:
        run = self._rows.get(run_id)
        if run is None or run.status not in expected:
            return None
        run = apply_changes(run, to, changes)
        self._rows[run_id] = run
        return run

    async def heartbeat(self, run_id: str, at: datetime) -> None:
        run = self._rows.get(run_id)
        if run is not None and run.status is RunStatus.RUNNING:
            self._rows[run_id] = replace(run, heartbeat_at=at)

    async def stale(self, before: datetime) -> Sequence[Run]:
        return [
            r
            for r in self._rows.values()
            if r.status is RunStatus.RUNNING and (r.heartbeat_at is None or r.heartbeat_at < before)
        ]


class InMemoryCredentialRepository:
    def __init__(self) -> None:
        self._rows: dict[str, CredentialSet] = {}

    def _check_name(self, cred: CredentialSet) -> None:
        for other in self._rows.values():
            if other.id != cred.id and other.name == cred.name:
                raise AlreadyExistsError(f"credential set {cred.name!r} already exists")

    async def add(self, cred: CredentialSet) -> None:
        self._check_name(cred)
        if cred.id in self._rows:
            raise AlreadyExistsError(f"credential set {cred.id!r} already exists")
        self._rows[cred.id] = replace(cred, is_default=False)

    async def get(self, cred_id: str) -> CredentialSet | None:
        return self._rows.get(cred_id)

    async def get_by_name(self, name: str) -> CredentialSet | None:
        return next((c for c in self._rows.values() if c.name == name), None)

    async def get_default(self) -> CredentialSet | None:
        return next((c for c in self._rows.values() if c.is_default), None)

    async def list(self) -> Sequence[CredentialSet]:
        return sorted(self._rows.values(), key=lambda c: c.name)

    async def update(self, cred: CredentialSet) -> None:
        cur = self._rows.get(cred.id)
        if cur is None:
            raise NotFoundError(f"credential set {cred.id!r} not found")
        self._check_name(cred)
        self._rows[cred.id] = replace(cred, is_default=cur.is_default)

    async def set_default(self, cred_id: str) -> None:
        if cred_id not in self._rows:
            raise NotFoundError(f"credential set {cred_id!r} not found")
        for k, c in self._rows.items():
            self._rows[k] = replace(c, is_default=k == cred_id)

    async def remove(self, cred_id: str) -> None:
        self._rows.pop(cred_id, None)


class InMemoryEventLog:
    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}
        self._changed = asyncio.Condition()

    async def append(self, event: Event) -> int:
        async with self._changed:
            stream = self._streams.setdefault(event.conversation_id, [])
            stored = replace(event, offset=len(stream) + 1)
            stream.append(stored)
            self._changed.notify_all()
            return stored.offset

    async def read(self, conversation_id: str, after: int, limit: int) -> Sequence[Event]:
        stream = self._streams.get(conversation_id, [])
        return stream[after : after + limit]

    async def tail(self, conversation_id: str, after: int) -> AsyncIterator[Event]:
        while True:
            batch = await self.read(conversation_id, after, 500)
            for event in batch:
                yield event
                after = event.offset
            if batch:
                continue
            async with self._changed:
                while len(self._streams.get(conversation_id, [])) <= after:
                    await self._changed.wait()


class InMemoryControlChannel:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[ControlMessage]]] = {}

    async def publish(self, message: ControlMessage) -> None:
        for q in self._subs.get(message.run_id, set()):
            q.put_nowait(message)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[AsyncIterator[ControlMessage]]:
        q: asyncio.Queue[ControlMessage] = asyncio.Queue()
        self._subs.setdefault(run_id, set()).add(q)

        async def messages() -> AsyncIterator[ControlMessage]:
            while True:
                yield await q.get()

        try:
            yield messages()
        finally:
            subs = self._subs.get(run_id, set())
            subs.discard(q)
            if not subs:
                self._subs.pop(run_id, None)


class InMemoryJobQueue:
    """Same semantics as the Redis adapter, for tests and single-process dev."""

    def __init__(self) -> None:
        self._pending: dict[str, deque[Job]] = {}
        self._active: dict[str, Job] = {}
        self._locks: dict[str, tuple[str, float]] = {}  # slug -> (token, expires)
        self._changed = asyncio.Condition()
        self.default_ttl = 30.0

    def _locked(self, slug: str) -> bool:
        lock = self._locks.get(slug)
        return lock is not None and lock[1] > time.monotonic()

    async def enqueue(self, job: Job) -> None:
        async with self._changed:
            self._pending.setdefault(job.agent_slug, deque()).append(job)
            self._changed.notify_all()

    def _try_claim(self) -> Lease | None:
        slugs = list(dict.fromkeys([*self._active, *self._pending]))
        for slug in slugs:
            if self._locked(slug):
                continue
            token = secrets.token_hex(8)
            if slug in self._active:
                self._locks[slug] = (token, time.monotonic() + self.default_ttl)
                return Lease(self._active[slug], token, recovered=True)
            queue = self._pending.get(slug)
            if queue:
                job = queue.popleft()
                if not queue:
                    del self._pending[slug]
                self._active[slug] = job
                self._locks[slug] = (token, time.monotonic() + self.default_ttl)
                return Lease(job, token)
        return None

    async def claim(self, consumer: str, timeout: float) -> Lease | None:
        deadline = time.monotonic() + timeout
        async with self._changed:
            while True:
                lease = self._try_claim()
                if lease is not None:
                    return lease
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Poll lock expiry as well as wake on enqueue/complete.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), min(remaining, 0.05))

    async def renew(self, lease: Lease, ttl: timedelta) -> bool:
        slug = lease.job.agent_slug
        lock = self._locks.get(slug)
        if lock is None or lock[0] != lease.token or lock[1] <= time.monotonic():
            return False
        self._locks[slug] = (lease.token, time.monotonic() + ttl.total_seconds())
        return True

    async def complete(self, lease: Lease) -> None:
        slug = lease.job.agent_slug
        async with self._changed:
            lock = self._locks.get(slug)
            if lock is None or lock[0] != lease.token:
                return
            self._active.pop(slug, None)
            del self._locks[slug]
            self._changed.notify_all()

    async def queued(self, agent_slug: str) -> Sequence[str]:
        return [j.run_id for j in self._pending.get(agent_slug, ())]

    async def recover(self) -> int:
        async with self._changed:
            n = sum(1 for slug in self._active if not self._locked(slug))
            if n:
                self._changed.notify_all()
            return n
