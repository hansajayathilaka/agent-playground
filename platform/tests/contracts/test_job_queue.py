"""Contract: every JobQueue adapter behaves the same.

Fixtures use a 0.5 s lock TTL so expiry paths run quickly.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from playground_core.models import Job
from playground_core.ports import JobQueue

TTL = timedelta(seconds=0.5)


async def test_claim_empty_times_out(job_queue: JobQueue) -> None:
    assert await job_queue.claim("w1", 0.1) is None


async def test_fifo_and_one_at_a_time_per_agent(job_queue: JobQueue) -> None:
    for i in range(3):
        await job_queue.enqueue(Job(f"a{i}", "alpha"))
    await job_queue.enqueue(Job("b0", "beta"))

    first = await job_queue.claim("w1", 1)
    second = await job_queue.claim("w2", 1)
    assert first is not None and second is not None
    got = {first.job.run_id, second.job.run_id}
    assert got == {"a0", "b0"}  # alpha's next job waits behind its lock
    alpha = first if first.job.agent_slug == "alpha" else second
    assert not alpha.recovered
    assert await job_queue.queued("alpha") == ["a1", "a2"]
    assert await job_queue.claim("w3", 0.2) is None

    await job_queue.complete(alpha)
    nxt = await job_queue.claim("w3", 1)
    assert nxt is not None and nxt.job.run_id == "a1"
    await job_queue.complete(nxt)
    last = await job_queue.claim("w3", 1)
    assert last is not None and last.job.run_id == "a2"
    assert await job_queue.queued("alpha") == []


async def test_enqueue_while_running_is_not_lost(job_queue: JobQueue) -> None:
    await job_queue.enqueue(Job("r1", "alpha"))
    lease = await job_queue.claim("w1", 1)
    assert lease is not None
    # The wake for r2 arrives while the lock is held and is dropped; the
    # holder's complete() must make r2 claimable.
    await job_queue.enqueue(Job("r2", "alpha"))
    assert await job_queue.claim("w2", 0.2) is None
    assert await job_queue.renew(lease, TTL)
    await job_queue.complete(lease)
    nxt = await job_queue.claim("w2", 1)
    assert nxt is not None and nxt.job.run_id == "r2"


async def test_dead_holder_job_comes_back_once_as_recovered(job_queue: JobQueue) -> None:
    await job_queue.enqueue(Job("r1", "alpha"))
    await job_queue.enqueue(Job("r2", "alpha"))
    lease = await job_queue.claim("w1", 1)
    assert lease is not None and lease.job.run_id == "r1"
    await asyncio.sleep(0.7)  # lock expires: holder "died"
    assert not await job_queue.renew(lease, TTL)
    await job_queue.recover()
    again = await job_queue.claim("w2", 1)
    assert again is not None
    assert again.job.run_id == "r1" and again.recovered
    # The dead holder's late complete() must not release the new holder's lock.
    await job_queue.complete(lease)
    assert await job_queue.claim("w3", 0.2) is None
    await job_queue.complete(again)
    nxt = await job_queue.claim("w3", 1)
    assert nxt is not None and nxt.job.run_id == "r2" and not nxt.recovered


async def test_renew_keeps_the_lock(job_queue: JobQueue) -> None:
    await job_queue.enqueue(Job("r1", "alpha"))
    lease = await job_queue.claim("w1", 1)
    assert lease is not None
    for _ in range(4):
        await asyncio.sleep(0.2)
        assert await job_queue.renew(lease, TTL)
    await job_queue.recover()
    assert await job_queue.claim("w2", 0.2) is None
    await job_queue.complete(lease)


async def test_claim_wakes_on_enqueue(job_queue: JobQueue) -> None:
    waiter = asyncio.create_task(job_queue.claim("w1", 2))
    await asyncio.sleep(0.1)
    await job_queue.enqueue(Job("r1", "alpha"))
    lease = await asyncio.wait_for(waiter, 2)
    assert lease is not None and lease.job.run_id == "r1"
