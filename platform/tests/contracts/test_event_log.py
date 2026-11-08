"""Contract: every EventLog adapter behaves the same."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from playground_core.events import Event, EventKind
from playground_core.ports import EventLog

T = datetime(2026, 11, 8, tzinfo=UTC)


def ev(conv: str, text: str) -> Event:
    return Event(conv, "run-1", EventKind.TEXT_DELTA, {"text": text}, "tool-1", T)


async def test_offsets_are_per_conversation(event_log: EventLog) -> None:
    assert await event_log.append(ev("c1", "a")) == 1
    assert await event_log.append(ev("c1", "b")) == 2
    assert await event_log.append(ev("c2", "x")) == 1
    got = await event_log.read("c1", 0, 10)
    assert [e.offset for e in got] == [1, 2]
    first = got[0]
    assert (first.kind, first.data, first.parent_tool_use_id, first.time) == (
        EventKind.TEXT_DELTA,
        {"text": "a"},
        "tool-1",
        T,
    )
    assert [e.offset for e in await event_log.read("c1", 1, 10)] == [2]
    assert [e.offset for e in await event_log.read("c1", 0, 1)] == [1]
    assert await event_log.read("missing", 0, 10) == []


async def test_tail_replays_then_follows(event_log: EventLog) -> None:
    await event_log.append(ev("c1", "a"))
    await event_log.append(ev("c1", "b"))
    seen: list[str] = []

    async def watch() -> None:
        async for e in event_log.tail("c1", 1):
            seen.append(e.data["text"])
            if len(seen) == 3:
                return

    task = asyncio.create_task(watch())
    await asyncio.sleep(0.1)
    await event_log.append(ev("c1", "c"))
    await asyncio.sleep(0.05)
    await event_log.append(ev("c1", "d"))
    await asyncio.wait_for(task, 3)
    assert seen == ["b", "c", "d"]


async def test_tail_can_be_cancelled_while_idle(event_log: EventLog) -> None:
    async def watch() -> None:
        async for _ in event_log.tail("idle", 0):
            pass

    task = asyncio.create_task(watch())
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_tail_survives_a_long_idle(event_log: EventLog) -> None:
    # Longer than redis-py's default 5 s socket timeout.
    async def first() -> Event:
        async for e in event_log.tail("quiet", 0):
            return e
        raise AssertionError("tail ended")

    task = asyncio.create_task(first())
    await asyncio.sleep(6)
    await event_log.append(ev("quiet", "late"))
    got = await asyncio.wait_for(task, 3)
    assert got.data["text"] == "late"
