"""Postgres repositories and a minimal forward-only migrator."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from importlib import resources
from typing import LiteralString, cast

from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.models import (
    AgentRecord,
    AuthType,
    Conversation,
    CredentialSet,
    Provider,
    Run,
    RunStatus,
    RunUsage,
)
from playground_core.ports import RunChanges

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


# -- Phase 2 ---------------------------------------------------------------------

_CONV_COLUMNS = "id, agent_slug, title, created_at, updated_at, session_id, credential_set_id"


def _conversation(row: TupleRow) -> Conversation:
    return Conversation(row[0], row[1], row[2], row[3], row[4], row[5], row[6])


class PostgresConversationRepository:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def add(self, conversation: Conversation) -> None:
        c = conversation
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"INSERT INTO conversations ({_CONV_COLUMNS}) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        c.id,
                        c.agent_slug,
                        c.title,
                        c.created_at,
                        c.updated_at,
                        c.session_id,
                        c.credential_set_id,
                    ),
                )
        except errors.UniqueViolation as e:
            raise AlreadyExistsError(f"conversation {c.id!r} already exists") from e

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_CONV_COLUMNS} FROM conversations WHERE id = %s", (conversation_id,)
            )
            row = await cur.fetchone()
        return _conversation(row) if row else None

    async def list(self, agent_slug: str) -> Sequence[Conversation]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_CONV_COLUMNS} FROM conversations WHERE agent_slug = %s"
                " ORDER BY updated_at DESC, id DESC",
                (agent_slug,),
            )
            return [_conversation(r) for r in await cur.fetchall()]

    async def update(self, conversation: Conversation) -> None:
        c = conversation
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE conversations SET title = %s, updated_at = %s, session_id = %s,"
                " credential_set_id = %s WHERE id = %s",
                (c.title, c.updated_at, c.session_id, c.credential_set_id, c.id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"conversation {c.id!r} not found")


_RUN_COLUMNS = (
    "id, conversation_id, agent_slug, prompt, status, created_at, started_at, finished_at,"
    " heartbeat_at, worker, error, credential_set_id, input_tokens, output_tokens, cost_usd,"
    " num_turns, duration_ms"
)


def _run(row: TupleRow) -> Run:
    return Run(
        id=row[0],
        conversation_id=row[1],
        agent_slug=row[2],
        prompt=row[3],
        status=RunStatus(row[4]),
        created_at=row[5],
        started_at=row[6],
        finished_at=row[7],
        heartbeat_at=row[8],
        worker=row[9],
        error=row[10],
        credential_set_id=row[11],
        usage=RunUsage(
            input_tokens=row[12],
            output_tokens=row[13],
            cost_usd=row[14],
            num_turns=row[15],
            duration_ms=row[16],
        ),
    )


class PostgresRunRepository:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def add(self, run: Run) -> None:
        r, u = run, run.usage
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"INSERT INTO runs ({_RUN_COLUMNS}) VALUES"
                    " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        r.id,
                        r.conversation_id,
                        r.agent_slug,
                        r.prompt,
                        r.status.value,
                        r.created_at,
                        r.started_at,
                        r.finished_at,
                        r.heartbeat_at,
                        r.worker,
                        r.error,
                        r.credential_set_id,
                        u.input_tokens,
                        u.output_tokens,
                        u.cost_usd,
                        u.num_turns,
                        u.duration_ms,
                    ),
                )
        except errors.UniqueViolation as e:
            raise AlreadyExistsError(f"run {r.id!r} already exists") from e

    async def get(self, run_id: str) -> Run | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE id = %s", (run_id,))
            row = await cur.fetchone()
        return _run(row) if row else None

    async def list(
        self, *, conversation_id: str = "", agent_slug: str = "", limit: int = 100
    ) -> Sequence[Run]:
        where: list[str] = []
        params: list[object] = []
        if conversation_id:
            where.append("conversation_id = %s")
            params.append(conversation_id)
        if agent_slug:
            where.append("agent_slug = %s")
            params.append(agent_slug)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = "seq" if conversation_id else "seq DESC"
        params.append(limit)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                cast(
                    LiteralString,
                    f"SELECT {_RUN_COLUMNS} FROM runs {clause} ORDER BY {order} LIMIT %s",
                ),
                params,
            )
            return [_run(r) for r in await cur.fetchall()]

    async def transition(
        self,
        run_id: str,
        expected: Collection[RunStatus],
        to: RunStatus,
        changes: RunChanges | None = None,
    ) -> Run | None:
        c = changes or RunChanges()
        sets = ["status = %s"]
        params: list[object] = [to.value]
        for name in ("started_at", "finished_at", "heartbeat_at", "worker", "error"):
            value = getattr(c, name)
            if value is not None:
                sets.append(f"{name} = %s")
                params.append(value)
        if c.credential_set_id is not None:
            sets.append("credential_set_id = %s")
            params.append(c.credential_set_id)
        if c.usage is not None:
            sets.append(
                "input_tokens = %s, output_tokens = %s, cost_usd = %s,"
                " num_turns = %s, duration_ms = %s"
            )
            u = c.usage
            params += [u.input_tokens, u.output_tokens, u.cost_usd, u.num_turns, u.duration_ms]
        params += [run_id, [s.value for s in expected]]
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                cast(
                    LiteralString,
                    f"UPDATE runs SET {', '.join(sets)} WHERE id = %s AND status = ANY(%s)"
                    f" RETURNING {_RUN_COLUMNS}",
                ),
                params,
            )
            row = await cur.fetchone()
        return _run(row) if row else None

    async def heartbeat(self, run_id: str, at: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET heartbeat_at = %s WHERE id = %s AND status = 'running'",
                (at, run_id),
            )

    async def stale(self, before: datetime) -> Sequence[Run]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE status = 'running'"
                " AND (heartbeat_at IS NULL OR heartbeat_at < %s)",
                (before,),
            )
            return [_run(r) for r in await cur.fetchall()]


_CRED_COLUMNS = (
    "id, name, description, provider, base_url, auth_type, headers, default_model,"
    " allowed_models, is_default, created_at, updated_at, secret, owner"
)


def _cred(row: TupleRow) -> CredentialSet:
    return CredentialSet(
        id=row[0],
        name=row[1],
        description=row[2],
        provider=Provider(row[3]),
        base_url=row[4],
        auth_type=AuthType(row[5]),
        headers=dict(row[6]),
        default_model=row[7],
        allowed_models=list(row[8]),
        is_default=row[9],
        created_at=row[10],
        updated_at=row[11],
        secret_ciphertext=bytes(row[12]),
        owner=row[13],
    )


class PostgresCredentialRepository:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def _one(self, where: LiteralString, value: object) -> CredentialSet | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_CRED_COLUMNS} FROM credential_sets WHERE {where}", (value,)
            )
            row = await cur.fetchone()
        return _cred(row) if row else None

    async def add(self, cred: CredentialSet) -> None:
        c = cred
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"INSERT INTO credential_sets ({_CRED_COLUMNS}) VALUES"
                    " (%s,%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s,%s,%s)",
                    (
                        c.id,
                        c.name,
                        c.description,
                        c.provider.value,
                        c.base_url,
                        c.auth_type.value,
                        Jsonb(c.headers),
                        c.default_model,
                        Jsonb(c.allowed_models),
                        c.created_at,
                        c.updated_at,
                        c.secret_ciphertext,
                        c.owner,
                    ),
                )
        except errors.UniqueViolation as e:
            raise AlreadyExistsError(f"credential set {c.name!r} already exists") from e

    async def get(self, cred_id: str) -> CredentialSet | None:
        return await self._one("id = %s", cred_id)

    async def get_by_name(self, name: str) -> CredentialSet | None:
        return await self._one("name = %s", name)

    async def get_default(self) -> CredentialSet | None:
        return await self._one("is_default = %s", True)

    async def list(self) -> Sequence[CredentialSet]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_CRED_COLUMNS} FROM credential_sets ORDER BY name")
            return [_cred(r) for r in await cur.fetchall()]

    async def update(self, cred: CredentialSet) -> None:
        c = cred
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE credential_sets SET name = %s, description = %s, provider = %s,"
                    " base_url = %s, auth_type = %s, headers = %s, default_model = %s,"
                    " allowed_models = %s, updated_at = %s, secret = %s, owner = %s"
                    " WHERE id = %s",
                    (
                        c.name,
                        c.description,
                        c.provider.value,
                        c.base_url,
                        c.auth_type.value,
                        Jsonb(c.headers),
                        c.default_model,
                        Jsonb(c.allowed_models),
                        c.updated_at,
                        c.secret_ciphertext,
                        c.owner,
                        c.id,
                    ),
                )
        except errors.UniqueViolation as e:
            raise AlreadyExistsError(f"credential set {c.name!r} already exists") from e
        if cur.rowcount == 0:
            raise NotFoundError(f"credential set {c.id!r} not found")

    async def set_default(self, cred_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute("SELECT 1 FROM credential_sets WHERE id = %s", (cred_id,))
            if await cur.fetchone() is None:
                raise NotFoundError(f"credential set {cred_id!r} not found")
            await conn.execute("UPDATE credential_sets SET is_default = false WHERE is_default")
            await conn.execute(
                "UPDATE credential_sets SET is_default = true WHERE id = %s", (cred_id,)
            )

    async def remove(self, cred_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM credential_sets WHERE id = %s", (cred_id,))
