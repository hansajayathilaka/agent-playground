"""Contract: every ControlChannel adapter behaves the same."""

from __future__ import annotations

import asyncio

from playground_core.models import ControlKind, ControlMessage
from playground_core.ports import ControlChannel


async def test_delivers_to_subscribers_of_that_run(control_channel: ControlChannel) -> None:
    async with control_channel.subscribe("r1") as messages:
        # Subscribed on enter: a publish right after must be delivered.
        await control_channel.publish(ControlMessage("r2", ControlKind.CANCEL))
        await control_channel.publish(ControlMessage("r1", ControlKind.CANCEL))
        msg = await asyncio.wait_for(anext(messages), 2)
        assert msg == ControlMessage("r1", ControlKind.CANCEL)


async def test_publish_without_subscribers_is_fine(control_channel: ControlChannel) -> None:
    await control_channel.publish(ControlMessage("nobody", ControlKind.CANCEL))


async def test_delivers_after_a_long_idle(control_channel: ControlChannel) -> None:
    # Longer than redis-py's default 5 s socket timeout.
    async with control_channel.subscribe("r1") as messages:

        async def first() -> ControlMessage:
            return await anext(messages)

        nxt = asyncio.create_task(first())
        await asyncio.sleep(6)
        await control_channel.publish(ControlMessage("r1", ControlKind.CANCEL))
        assert await asyncio.wait_for(nxt, 3) == ControlMessage("r1", ControlKind.CANCEL)
