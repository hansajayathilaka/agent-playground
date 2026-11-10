"""Ports: the interfaces adapters implement.

Each port has a contract test suite in platform/tests/contracts that every
adapter must pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from playground_core.events import Event, EventKind
from playground_core.models import (
    Actor,
    AgentRecord,
    Commit,
    ControlMessage,
    Conversation,
    CredentialSet,
    FileChange,
    FileContent,
    FileEntry,
    Job,
    Lease,
    Principal,
    Run,
    RunLimits,
    RunStatus,
    RunUsage,
    Template,
    WriteResult,
)


class AgentStore(Protocol):
    """File CRUD inside agent folders. Paths are client strings relative to
    the agent root and must be resolved with resolve_agent_path()."""

    async def create_agent(self, slug: str, files: Mapping[str, bytes]) -> None:
        """Create the folder with initial files. AlreadyExistsError if present."""
        ...

    async def trash_agent(self, slug: str) -> None:
        """Move the folder out of the agent root into the trash."""
        ...

    async def list_agent_slugs(self) -> Sequence[str]: ...

    async def exists(self, slug: str) -> bool: ...

    async def tree(self, slug: str, *, include_hidden: bool) -> Sequence[FileEntry]: ...

    async def read(self, slug: str, path: str) -> FileContent: ...

    async def write(
        self, slug: str, path: str, data: bytes, *, if_match: str = "", create_only: bool = False
    ) -> WriteResult:
        """Atomic write. ConflictError on etag mismatch, AlreadyExistsError
        when create_only and the file exists."""
        ...

    async def delete(self, slug: str, path: str, *, recursive: bool) -> None: ...

    async def move(self, slug: str, from_path: str, to_path: str) -> FileEntry: ...

    async def mkdir(self, slug: str, path: str) -> FileEntry: ...


class AgentRepository(Protocol):
    """The agent index (fast listing and metadata)."""

    async def add(self, record: AgentRecord) -> None:
        """AlreadyExistsError if the slug is taken."""
        ...

    async def get(self, slug: str) -> AgentRecord | None: ...

    async def list(self) -> Sequence[AgentRecord]:
        """All agents ordered by slug."""
        ...

    async def update(self, record: AgentRecord) -> None:
        """NotFoundError if the slug is unknown."""
        ...

    async def remove(self, slug: str) -> None:
        """No-op if missing."""
        ...


class TemplateCatalog(Protocol):
    async def list(self) -> Sequence[Template]: ...

    async def get(self, name: str) -> Template:
        """NotFoundError if unknown."""
        ...


class SessionStore(Protocol):
    """Login sessions. Implementations store a hash of the token, never the
    token itself."""

    async def put(self, token: str, principal: Principal, ttl: timedelta) -> None: ...

    async def get(self, token: str) -> Principal | None:
        """None when missing or expired."""
        ...

    async def delete(self, token: str) -> None: ...


class AuthProvider(Protocol):
    """Checks login credentials (v1: one admin user)."""

    async def authenticate(self, username: str, password: str) -> Principal | None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


# -- Phase 2: runs -------------------------------------------------------------


class JobQueue(Protocol):
    """Run jobs with one-at-a-time execution per agent, in enqueue order.

    ``claim`` returns a lease only together with that agent's lock, so a
    holder of a lease is the only one running that agent. Jobs are delivered
    at most once to a live worker: a job whose holder died comes back exactly
    once with ``recovered=True`` (the platform never re-runs it blindly).
    """

    async def enqueue(self, job: Job) -> None: ...

    async def claim(self, consumer: str, timeout: float) -> Lease | None:
        """Wait up to ``timeout`` seconds for a runnable job."""
        ...

    async def renew(self, lease: Lease, ttl: timedelta) -> bool:
        """Extend the agent lock. False if it was lost (expired and taken)."""
        ...

    async def complete(self, lease: Lease) -> None:
        """Drop the job, release the agent lock, and make the agent's next
        queued job claimable."""
        ...

    async def queued(self, agent_slug: str) -> Sequence[str]:
        """Run ids waiting for this agent, oldest first (not the running one)."""
        ...

    async def recover(self) -> int:
        """Make jobs whose holder died claimable again. Returns how many
        agents were woken. Safe to call from every worker periodically."""
        ...

    async def lock(self, agent_slug: str, ttl: timedelta) -> str | None:
        """Take the agent lock without a job, for edits that must not overlap
        a run (Builder writes, rollbacks). Returns a token, or None when the
        agent is busy. Queued jobs wait until ``unlock``."""
        ...

    async def unlock(self, agent_slug: str, token: str) -> None:
        """Release a lock taken with ``lock`` and make the agent's next job
        claimable. No-op if the token no longer holds the lock."""
        ...


class EventLog(Protocol):
    """Append-only event stream per conversation, replayable by offset."""

    async def append(self, event: Event) -> int:
        """Store the event; returns its offset (1, 2, 3, ... per conversation)."""
        ...

    async def read(self, conversation_id: str, after: int, limit: int) -> Sequence[Event]:
        """Events with offset > after, oldest first, at most ``limit``."""
        ...

    def tail(self, conversation_id: str, after: int) -> AsyncIterator[Event]:
        """Replay from ``after`` and then follow new events until cancelled."""
        ...


class ControlChannel(Protocol):
    """Commands from the API to the worker running a given run."""

    async def publish(self, message: ControlMessage) -> None: ...

    def subscribe(self, run_id: str) -> AbstractAsyncContextManager[AsyncIterator[ControlMessage]]:
        """Subscribed once ``__aenter__`` returns: every message published for
        ``run_id`` after that is delivered to the iterator until exit."""
        ...


class ConversationRepository(Protocol):
    async def add(self, conversation: Conversation) -> None: ...

    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def list(self, agent_slug: str) -> Sequence[Conversation]:
        """Newest first (by updated_at)."""
        ...

    async def update(self, conversation: Conversation) -> None:
        """NotFoundError if unknown."""
        ...


@dataclass(frozen=True, slots=True)
class RunChanges:
    """Fields a status transition may set (None = leave unchanged)."""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    worker: str | None = None
    error: str | None = None
    credential_set_id: str | None = None
    usage: RunUsage | None = None


class RunRepository(Protocol):
    async def add(self, run: Run) -> None: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list(
        self, *, conversation_id: str = "", agent_slug: str = "", limit: int = 100
    ) -> Sequence[Run]:
        """Oldest first within a conversation; newest first otherwise."""
        ...

    async def transition(
        self,
        run_id: str,
        expected: Collection[RunStatus],
        to: RunStatus,
        changes: RunChanges | None = None,
    ) -> Run | None:
        """Compare-and-set the status. Returns the updated run, or None when
        the run is missing or its status is not in ``expected``."""
        ...

    async def heartbeat(self, run_id: str, at: datetime) -> None: ...

    async def stale(self, before: datetime) -> Sequence[Run]:
        """Running runs whose heartbeat is older than ``before``."""
        ...


class CredentialRepository(Protocol):
    async def add(self, cred: CredentialSet) -> None:
        """AlreadyExistsError on a duplicate name."""
        ...

    async def get(self, cred_id: str) -> CredentialSet | None: ...

    async def get_by_name(self, name: str) -> CredentialSet | None: ...

    async def get_default(self) -> CredentialSet | None: ...

    async def list(self) -> Sequence[CredentialSet]:
        """Ordered by name."""
        ...

    async def update(self, cred: CredentialSet) -> None:
        """NotFoundError if unknown, AlreadyExistsError on a duplicate name."""
        ...

    async def set_default(self, cred_id: str) -> None:
        """Make this the only default. NotFoundError if unknown."""
        ...

    async def remove(self, cred_id: str) -> None: ...


class SecretStore(Protocol):
    """Encrypts secrets at rest. Ciphertext is opaque to everything else."""

    def encrypt(self, plaintext: bytes, *, context: str) -> bytes:
        """``context`` is bound to the ciphertext (AEAD associated data), so a
        ciphertext copied to another record fails to decrypt."""
        ...

    def decrypt(self, ciphertext: bytes, *, context: str) -> bytes: ...


# -- Phase 4: agent history -------------------------------------------------------


class AgentHistory(Protocol):
    """Version history of each agent folder (v1: a git repository in the
    folder's ``.git/``). ``state/`` is never part of the history."""

    async def commit(self, slug: str, message: str, actor: Actor) -> Commit | None:
        """Snapshot the whole folder. Starts the history if there is none.
        Returns None when nothing changed since the last commit."""
        ...

    async def log(self, slug: str, *, limit: int, path: str = "") -> Sequence[Commit]:
        """Newest first; only commits touching ``path`` when given."""
        ...

    async def diff(self, slug: str, from_commit: str, to_commit: str) -> Sequence[FileChange]:
        """Changes from ``from_commit`` to ``to_commit``. Empty ``to_commit``
        is the latest commit; empty ``from_commit`` is ``to_commit``'s parent
        (or nothing, for the first commit). NotFoundError for unknown ids."""
        ...

    async def restore(self, slug: str, commit_id: str, path: str = "") -> None:
        """Make the folder (or just ``path``) match ``commit_id``: changed
        files are rewritten, files added since are removed. Does not commit."""
        ...


# -- AgentRunner -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    conversation_id: str
    agent_slug: str
    prompt: str
    # SDK session to resume; empty starts a new session.
    resume_session_id: str
    model: str | None
    max_turns: int | None
    max_budget_usd: float | None
    # Provider env for this run only (credentials included). The runner must
    # not add the worker's own environment to it.
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    limits: RunLimits = field(default_factory=RunLimits)
    # Hosts the run may reach; empty means open internet (decision 14).
    network_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_id: str
    status: RunStatus  # SUCCEEDED, FAILED or CANCELLED
    error: str = ""
    usage: RunUsage = field(default_factory=RunUsage)


class EventSink(Protocol):
    async def __call__(
        self, kind: EventKind, data: Mapping[str, Any], *, parent_tool_use_id: str = ""
    ) -> None: ...


class AgentRunner(Protocol):
    """Executes one agent turn."""

    async def run(
        self,
        request: RunRequest,
        emit: EventSink,
        cancel: asyncio.Event,
    ) -> RunOutcome:
        """Run the turn, emitting events as they happen. When ``cancel`` is
        set the runner must stop promptly and return status CANCELLED."""
        ...
