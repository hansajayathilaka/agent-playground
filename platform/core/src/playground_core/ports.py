"""Ports: the interfaces adapters implement.

Each port has a contract test suite in platform/tests/contracts that every
adapter must pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from playground_core.models import (
    AgentRecord,
    FileContent,
    FileEntry,
    Principal,
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
