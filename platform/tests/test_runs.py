"""Conversations, runs and the worker end to end on in-memory adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from playground_core.errors import FailedPreconditionError, InvalidArgumentError, NotFoundError
from playground_core.events import EventKind
from playground_core.models import ActorKind, Job, RunStatus
from playground_core.ports import RunChanges
from playground_core.services.history import edit_lock

from .runs_harness import (
    FakeRunner,
    Platform,
    add_default_credentials,
    make_platform,
    memory_infra,
    running,
)

DONE = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}


@pytest.fixture
async def platform(agents_root: Path) -> AsyncIterator[Platform]:
    p = make_platform(memory_infra(agents_root), FakeRunner())
    await p.agents.create("alpha", "Alpha", "", "blank")
    await p.agents.create("beta", "Beta", "", "blank")
    yield p


async def test_turns_resume_the_same_session(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    async with running(p.worker):
        r1 = await p.conversations.send_message(conv.id, "hello")
        assert await p.wait_status(r1.id, DONE) is RunStatus.SUCCEEDED
        r2 = await p.conversations.send_message(conv.id, "again")
        assert await p.wait_status(r2.id, DONE) is RunStatus.SUCCEEDED

    first, second = p.runner.requests
    assert first.resume_session_id == ""
    assert second.resume_session_id == f"session-{conv.id}"
    assert first.model == "claude-test"
    # Credentials reach the runner as env only, decrypted for this run.
    assert first.env == {"ANTHROPIC_API_KEY": "sk-test-secret"}

    details = await p.conversations.get(conv.id)
    assert details.conversation.title == "hello"
    assert [r.status for r in details.runs] == [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED]
    assert details.runs[0].credential_set_id != ""

    events = await p.events(conv.id)
    assert [e.offset for e in events] == list(range(1, len(events) + 1))
    kinds = [e.kind for e in events if e.run_id == r1.id]
    assert kinds == [
        EventKind.USER_MESSAGE,
        EventKind.RUN_STATUS,  # queued
        EventKind.RUN_STATUS,  # running
        EventKind.TEXT_DELTA,
        EventKind.ASSISTANT_TEXT,
        EventKind.TOOL_CALL,
        EventKind.ASSISTANT_TEXT,
        EventKind.RUN_STATUS,  # succeeded
    ]
    statuses = [e.data["status"] for e in events if e.kind is EventKind.RUN_STATUS]
    assert statuses[:3] == ["queued", "running", "succeeded"]
    nested = [e for e in events if e.parent_tool_use_id == "t1"]
    assert [e.data["text"] for e in nested][:1] == ["from subagent"]


async def test_one_run_per_agent_in_order(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    a1 = await p.conversations.create("alpha")
    a2 = await p.conversations.create("alpha")
    b = await p.conversations.create("beta")
    async with running(p.worker):
        first = await p.conversations.send_message(a1.id, "wait 1")
        await asyncio.wait_for(p.runner.started.wait(), 5)
        second = await p.conversations.send_message(a2.id, "second")
        other = await p.conversations.send_message(b.id, "other agent")
        # beta runs while alpha is busy; alpha's second turn waits its turn.
        assert await p.wait_status(other.id, DONE) is RunStatus.SUCCEEDED
        assert (await p.conversations.get_run(second.id)).status is RunStatus.QUEUED
        assert await p.conversations.queued_runs("alpha") == [second.id]
        p.runner.release.set()
        assert await p.wait_status(first.id, DONE) is RunStatus.SUCCEEDED
        assert await p.wait_status(second.id, DONE) is RunStatus.SUCCEEDED
    prompts = [r.prompt for r in p.runner.requests if r.agent_slug == "alpha"]
    assert prompts == ["wait 1", "second"]


async def test_cancel_running_run(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    async with running(p.worker):
        run = await p.conversations.send_message(conv.id, "wait for cancel")
        await asyncio.wait_for(p.runner.started.wait(), 5)
        await p.conversations.cancel(run.id)
        assert await p.wait_status(run.id, DONE) is RunStatus.CANCELLED
        # Idempotent on a finished run.
        assert (await p.conversations.cancel(run.id)).status is RunStatus.CANCELLED
        # The session is kept, so the next turn resumes it.
        nxt = await p.conversations.send_message(conv.id, "after")
        assert await p.wait_status(nxt.id, DONE) is RunStatus.SUCCEEDED
    assert p.runner.requests[-1].resume_session_id == f"session-{conv.id}"


async def test_cancel_queued_run_never_starts(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    run = await p.conversations.send_message(conv.id, "never")
    cancelled = await p.conversations.cancel(run.id)
    assert cancelled.status is RunStatus.CANCELLED
    async with running(p.worker):
        follow = await p.conversations.send_message(conv.id, "next")
        assert await p.wait_status(follow.id, DONE) is RunStatus.SUCCEEDED
    assert [r.prompt for r in p.runner.requests] == ["next"]


async def test_failures_are_reported(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    async with running(p.worker):
        failed = await p.conversations.send_message(conv.id, "fail")
        crashed = await p.conversations.send_message(conv.id, "boom")
        assert await p.wait_status(failed.id, DONE) is RunStatus.FAILED
        assert await p.wait_status(crashed.id, DONE) is RunStatus.FAILED
    assert (await p.conversations.get_run(failed.id)).error == "model said no"
    assert (await p.conversations.get_run(crashed.id)).error == "runner crashed"


async def test_send_checks_preconditions(platform: Platform) -> None:
    p = platform
    conv = await p.conversations.create("alpha")
    with pytest.raises(FailedPreconditionError, match="no credential set"):
        await p.conversations.send_message(conv.id, "hi")
    await add_default_credentials(p.credentials)
    with pytest.raises(InvalidArgumentError):
        await p.conversations.send_message(conv.id, "   ")
    with pytest.raises(NotFoundError):
        await p.conversations.send_message("nope", "hi")
    await p.infra.store.write("alpha", "agent.json", b"{not json")
    with pytest.raises(FailedPreconditionError, match="validation errors"):
        await p.conversations.send_message(conv.id, "hi")
    await p.infra.store.write(
        "alpha", "agent.json", b'{"name": "Alpha", "credential_set": "missing"}'
    )
    with pytest.raises(FailedPreconditionError, match="missing"):
        await p.conversations.send_message(conv.id, "hi")


async def test_credentials_removed_after_queueing_fail_the_run(platform: Platform) -> None:
    p = platform
    cred_id = await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    run = await p.conversations.send_message(conv.id, "hi", credential_set_id=cred_id)
    await p.infra.credentials.remove(cred_id)
    async with running(p.worker):
        assert await p.wait_status(run.id, DONE) is RunStatus.FAILED
    assert "no longer exists" in (await p.conversations.get_run(run.id)).error


async def test_recovered_running_job_is_interrupted(platform: Platform) -> None:
    """A worker died mid-run: the next claim gets the job back as recovered
    and marks it interrupted instead of re-running it."""
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    run = await p.conversations.send_message(conv.id, "hi")
    queue = p.infra.queue
    lease = await queue.claim("dead-worker", 1)
    assert lease is not None
    await p.infra.runs.transition(
        run.id, {RunStatus.QUEUED}, RunStatus.RUNNING, RunChanges(worker="dead-worker")
    )
    # The dead worker's lease runs out.
    await queue.renew(lease, timedelta(seconds=0.05))
    await asyncio.sleep(0.1)
    async with running(p.worker):
        assert await p.wait_status(run.id, DONE) is RunStatus.INTERRUPTED
    assert p.runner.requests == []


async def test_reaper_interrupts_stale_runs(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    run = await p.conversations.send_message(conv.id, "hi")
    old = p.infra.clock.now() - timedelta(minutes=10)
    await p.infra.runs.transition(
        run.id, {RunStatus.QUEUED}, RunStatus.RUNNING, RunChanges(heartbeat_at=old)
    )
    await p.worker.reap_once()
    assert (await p.conversations.get_run(run.id)).status is RunStatus.INTERRUPTED
    events = await p.events(conv.id)
    assert events[-1].data["status"] == "interrupted"


async def test_unknown_job_is_dropped(platform: Platform) -> None:
    p = platform
    await p.infra.queue.enqueue(Job("ghost", "alpha"))
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    async with running(p.worker):
        run = await p.conversations.send_message(conv.id, "real")
        assert await p.wait_status(run.id, DONE) is RunStatus.SUCCEEDED


async def test_watch_replays_and_follows(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    conv = await p.conversations.create("alpha")
    await p.conversations.send_message(conv.id, "hello")
    got: list[int] = []

    async def watch() -> None:
        async for e in p.conversations.watch(conv.id, 1):
            got.append(e.offset)
            if e.kind is EventKind.RUN_STATUS and e.data["status"] == "succeeded":
                return

    async with running(p.worker):
        await asyncio.wait_for(watch(), 5)
    assert got[0] == 2 and got == list(range(2, len(got) + 2))


async def test_run_changes_are_recorded_as_the_agent(platform: Platform, agents_root: Path) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    _, run = await p.conversations.start_run("alpha", "wait and write")
    async with running(p.worker):
        await asyncio.wait_for(p.runner.started.wait(), 5)
        (agents_root / "alpha" / "workspace" / "out.txt").write_text("result")
        p.runner.release.set()
        assert await p.wait_status(run.id, DONE) is RunStatus.SUCCEEDED
    head = (await p.infra.history.log("alpha", limit=1))[0]
    assert head.message == "Run: wait and write"
    assert head.actor is ActorKind.AGENT and head.run_id == run.id
    changes = await p.infra.history.diff("alpha", "", "")
    assert [c.path for c in changes] == ["workspace/out.txt"]


async def test_edits_wait_for_the_running_turn(platform: Platform) -> None:
    p = platform
    await add_default_credentials(p.credentials)
    await p.conversations.start_run("alpha", "wait for me")
    async with running(p.worker):
        await asyncio.wait_for(p.runner.started.wait(), 5)
        with pytest.raises(FailedPreconditionError, match="is running"):
            async with edit_lock(p.infra.queue, "alpha", wait=0.3):
                pass
        waiter = asyncio.create_task(_edit_after_run(p))
        await asyncio.sleep(0.3)
        assert not waiter.done()
        p.runner.release.set()
        assert await asyncio.wait_for(waiter, 5) == RunStatus.SUCCEEDED
        # While an edit holds the agent, its next turn waits.
        async with edit_lock(p.infra.queue, "alpha", wait=1):
            _, second = await p.conversations.start_run("alpha", "next")
            await asyncio.sleep(0.3)
            assert (await p.conversations.get_run(second.id)).status is RunStatus.QUEUED
        assert await p.wait_status(second.id, DONE) is RunStatus.SUCCEEDED


async def _edit_after_run(p: Platform) -> RunStatus:
    async with edit_lock(p.infra.queue, "alpha", wait=5):
        runs = await p.conversations.list_runs(agent_slug="alpha")
        return runs[0].status
