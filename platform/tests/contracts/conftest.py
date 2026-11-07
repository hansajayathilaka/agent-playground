"""Adapter fixtures for the port contract suites.

Each fixture is parametrized over every adapter of a port. Postgres and Redis
adapters run when TEST_DATABASE_URL / TEST_REDIS_URL are set (CI sets both)
and are skipped otherwise.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import InMemoryAgentRepository, InMemorySessionStore
from playground_adapters.postgres import Pool, PostgresAgentRepository, migrate
from playground_adapters.redis_sessions import RedisSessionStore
from playground_core.ports import AgentRepository, AgentStore, SessionStore

from ..conftest import env_or_skip


@pytest.fixture(scope="session")
async def pg_pool() -> AsyncIterator[Pool | None]:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        yield None
        return
    pool: Pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open(wait=True, timeout=10)
    async with pool.connection() as conn:
        await conn.execute("DROP TABLE IF EXISTS agents, schema_migrations")
    assert await migrate(pool) == ["0001_agents.sql"]
    assert await migrate(pool) == []  # idempotent
    yield pool
    await pool.close()


@pytest.fixture(params=["memory", "postgres"])
async def agent_repo(request: pytest.FixtureRequest, pg_pool: Pool | None) -> AgentRepository:
    if request.param == "memory":
        return InMemoryAgentRepository()
    if pg_pool is None:
        pytest.skip("TEST_DATABASE_URL not set")
    pool = pg_pool
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE agents")
    return PostgresAgentRepository(pool)


@pytest.fixture(params=["memory", "redis"])
async def session_store(request: pytest.FixtureRequest) -> AsyncIterator[SessionStore]:
    if request.param == "memory":
        yield InMemorySessionStore()
        return
    client = Redis.from_url(env_or_skip("TEST_REDIS_URL"))  # pyright: ignore[reportUnknownMemberType]
    prefix = f"test:{request.node.name}:"  # pyright: ignore[reportUnknownMemberType]
    yield RedisSessionStore(client, prefix=prefix)
    await client.aclose()


@pytest.fixture(params=["local-fs"])
def agent_store(request: pytest.FixtureRequest, tmp_path: Path) -> AgentStore:
    return LocalAgentStore(tmp_path / "agents")
