"""Postgres repositories and a minimal forward-only migrator."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from typing import LiteralString, cast

from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.models import AgentRecord

Pool = AsyncConnectionPool[AsyncConnection[TupleRow]]

_COLUMNS = "slug, name, description, template, created_at, updated_at"


async def migrate(pool: Pool) -> list[str]:
    """Apply migrations/NNNN_*.sql in order, once each. Returns applied names."""
    files = sorted(
        (
            f
            for f in resources.files("playground_adapters.migrations").iterdir()
            if f.name.endswith(".sql")
        ),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    async with pool.connection() as conn, conn.transaction():
        # Serialize concurrent migrators (several API replicas starting at once).
        await conn.execute("SELECT pg_advisory_xact_lock(727274)")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT name FROM schema_migrations")
        done = {row[0] for row in await cur.fetchall()}
        for f in files:
            if f.name in done:
                continue
            # Migration files ship inside this package; they are trusted SQL.
            await conn.execute(cast(LiteralString, f.read_text()))
            await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


def _record(row: TupleRow) -> AgentRecord:
    return AgentRecord(row[0], row[1], row[2], row[3], row[4], row[5])


class PostgresAgentRepository:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def add(self, record: AgentRecord) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"INSERT INTO agents ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        record.slug,
                        record.name,
                        record.description,
                        record.template,
                        record.created_at,
                        record.updated_at,
                    ),
                )
        except errors.UniqueViolation as e:
            raise AlreadyExistsError(f"agent {record.slug!r} already exists") from e

    async def get(self, slug: str) -> AgentRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_COLUMNS} FROM agents WHERE slug = %s", (slug,))
            row = await cur.fetchone()
        return _record(row) if row else None

    async def list(self) -> Sequence[AgentRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_COLUMNS} FROM agents ORDER BY slug")
            return [_record(r) for r in await cur.fetchall()]

    async def update(self, record: AgentRecord) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE agents SET name = %s, description = %s, template = %s, updated_at = %s"
                " WHERE slug = %s",
                (record.name, record.description, record.template, record.updated_at, record.slug),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"agent {record.slug!r} not found")

    async def remove(self, slug: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM agents WHERE slug = %s", (slug,))
