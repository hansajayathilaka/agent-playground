"""In-memory adapters for tests and single-process development."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from datetime import timedelta

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.models import AgentRecord, Principal


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
