"""Contracts: RunRepository, ConversationRepository, CredentialRepository."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.models import (
    AuthType,
    Conversation,
    CredentialSet,
    Provider,
    Run,
    RunStatus,
    RunUsage,
)
from playground_core.ports import (
    ConversationRepository,
    CredentialRepository,
    RunChanges,
    RunRepository,
)

T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def conv(cid: str, slug: str = "alpha", at: datetime = T0) -> Conversation:
    return Conversation(cid, slug, f"title {cid}", at, at)


async def test_conversations(conversation_repo: ConversationRepository) -> None:
    await conversation_repo.add(conv("c1"))
    await conversation_repo.add(conv("c2", at=T0 + timedelta(minutes=1)))
    await conversation_repo.add(conv("c3", slug="beta"))
    with pytest.raises(AlreadyExistsError):
        await conversation_repo.add(conv("c1"))
    assert [c.id for c in await conversation_repo.list("alpha")] == ["c2", "c1"]
    c1 = await conversation_repo.get("c1")
    assert c1 == conv("c1")
    updated = replace(conv("c1"), session_id="s-1", updated_at=T0 + timedelta(minutes=5))
    await conversation_repo.update(updated)
    assert await conversation_repo.get("c1") == updated
    assert [c.id for c in await conversation_repo.list("alpha")] == ["c1", "c2"]
    with pytest.raises(NotFoundError):
        await conversation_repo.update(conv("nope"))
    assert await conversation_repo.get("nope") is None


def run(rid: str, cid: str = "c1", slug: str = "alpha") -> Run:
    return Run(rid, cid, slug, f"prompt {rid}", RunStatus.QUEUED, T0)


async def test_runs(conversation_repo: ConversationRepository, run_repo: RunRepository) -> None:
    # Postgres has a foreign key to conversations; memory doesn't care.
    for c in ("c1", "c2"):
        with contextlib.suppress(AlreadyExistsError):
            await conversation_repo.add(conv(c))
    if type(run_repo).__name__.startswith("Postgres") and not type(
        conversation_repo
    ).__name__.startswith("Postgres"):
        pytest.skip("postgres runs need postgres conversations")
    await run_repo.add(run("r1"))
    await run_repo.add(run("r2"))
    await run_repo.add(run("r3", cid="c2", slug="beta"))
    assert await run_repo.get("r1") == run("r1")
    assert [r.id for r in await run_repo.list(conversation_id="c1")] == ["r1", "r2"]
    assert [r.id for r in await run_repo.list(agent_slug="alpha")] == ["r2", "r1"]
    assert [r.id for r in await run_repo.list(limit=2)] == ["r3", "r2"]

    started = await run_repo.transition(
        "r1",
        {RunStatus.QUEUED},
        RunStatus.RUNNING,
        RunChanges(started_at=T0, heartbeat_at=T0, worker="w1"),
    )
    assert started is not None and started.status is RunStatus.RUNNING
    assert started.worker == "w1" and started.error == ""
    # Compare-and-set: a second claim fails.
    assert await run_repo.transition("r1", {RunStatus.QUEUED}, RunStatus.RUNNING) is None
    assert await run_repo.transition("missing", {RunStatus.QUEUED}, RunStatus.RUNNING) is None

    assert [r.id for r in await run_repo.stale(T0 + timedelta(seconds=1))] == ["r1"]
    await run_repo.heartbeat("r1", T0 + timedelta(seconds=5))
    assert await run_repo.stale(T0 + timedelta(seconds=1)) == []

    usage = RunUsage(10, 20, 0.5, 3, 1234)
    done = await run_repo.transition(
        "r1",
        {RunStatus.RUNNING},
        RunStatus.SUCCEEDED,
        RunChanges(finished_at=T0 + timedelta(seconds=9), usage=usage, credential_set_id="cs"),
    )
    assert done is not None
    assert (done.status, done.usage, done.credential_set_id, done.worker) == (
        RunStatus.SUCCEEDED,
        usage,
        "cs",
        "w1",
    )
    assert await run_repo.get("r1") == done


def cred(cid: str, name: str) -> CredentialSet:
    return CredentialSet(
        id=cid,
        name=name,
        description="d",
        provider=Provider.GATEWAY,
        base_url="https://example.test",
        auth_type=AuthType.BEARER,
        headers={"X-Team": "a"},
        default_model="m1",
        allowed_models=["m1", "m2"],
        is_default=False,
        created_at=T0,
        updated_at=T0,
        secret_ciphertext=b"\x01secret",
    )


async def test_credentials(credential_repo: CredentialRepository) -> None:
    await credential_repo.add(cred("1", "beta"))
    await credential_repo.add(cred("2", "alpha"))
    with pytest.raises(AlreadyExistsError):
        await credential_repo.add(cred("3", "alpha"))
    assert [c.name for c in await credential_repo.list()] == ["alpha", "beta"]
    assert await credential_repo.get("1") == cred("1", "beta")
    assert (await credential_repo.get_by_name("alpha")) == cred("2", "alpha")
    assert await credential_repo.get_default() is None

    await credential_repo.set_default("1")
    await credential_repo.set_default("2")
    default = await credential_repo.get_default()
    assert default is not None and default.id == "2"
    assert [c.is_default for c in await credential_repo.list()] == [True, False]
    with pytest.raises(NotFoundError):
        await credential_repo.set_default("nope")

    renamed = replace(cred("1", "gamma"), description="new", secret_ciphertext=b"\x01x")
    await credential_repo.update(renamed)
    assert await credential_repo.get("1") == renamed
    with pytest.raises(AlreadyExistsError):
        await credential_repo.update(replace(renamed, name="alpha"))
    with pytest.raises(NotFoundError):
        await credential_repo.update(cred("nope", "zzz"))
    # update never changes the default flag
    await credential_repo.update(replace(cred("2", "alpha"), is_default=False))
    d = await credential_repo.get_default()
    assert d is not None and d.id == "2"

    await credential_repo.remove("1")
    await credential_repo.remove("1")
    assert await credential_repo.get("1") is None
