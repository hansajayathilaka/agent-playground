"""Contract: every AgentRepository adapter behaves the same."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.models import AgentRecord
from playground_core.ports import AgentRepository

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def rec(slug: str, name: str = "N") -> AgentRecord:
    return AgentRecord(slug, name, "desc", "blank", T0, T0)


async def test_add_get(agent_repo: AgentRepository) -> None:
    await agent_repo.add(rec("a"))
    assert await agent_repo.get("a") == rec("a")
    assert await agent_repo.get("missing") is None


async def test_add_duplicate(agent_repo: AgentRepository) -> None:
    await agent_repo.add(rec("a"))
    with pytest.raises(AlreadyExistsError):
        await agent_repo.add(rec("a", "other"))


async def test_list_is_sorted(agent_repo: AgentRepository) -> None:
    for slug in ["c", "a", "b"]:
        await agent_repo.add(rec(slug))
    assert [r.slug for r in await agent_repo.list()] == ["a", "b", "c"]


async def test_update(agent_repo: AgentRepository) -> None:
    await agent_repo.add(rec("a"))
    changed = replace(rec("a"), name="New", updated_at=T0 + timedelta(minutes=5))
    await agent_repo.update(changed)
    assert await agent_repo.get("a") == changed
    with pytest.raises(NotFoundError):
        await agent_repo.update(rec("ghost"))


async def test_remove_is_idempotent(agent_repo: AgentRepository) -> None:
    await agent_repo.add(rec("a"))
    await agent_repo.remove("a")
    await agent_repo.remove("a")
    assert await agent_repo.get("a") is None
    await agent_repo.add(rec("a"))  # slug reusable
