"""The worker process loop: N claim slots, a reaper, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from playground_core.events import EventKind
from playground_core.models import Lease, RunStatus
from playground_core.ports import Clock, JobQueue, RunChanges, RunRepository
from playground_core.services.conversations import status_event
from playground_core.services.execution import RunExecutor

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        executor: RunExecutor,
        queue: JobQueue,
        runs: RunRepository,
        clock: Clock,
        worker_id: str,
        concurrency: int,
        stale_after: timedelta = timedelta(seconds=90),
        reaper_interval: float = 15.0,
        claim_timeout: float = 5.0,
    ) -> None:
        self._executor = executor
        self._queue = queue
        self._runs = runs
        self._clock = clock
        self._id = worker_id
        self._concurrency = concurrency
        self._stale_after = stale_after
        self._reaper_interval = reaper_interval
        self._claim_timeout = claim_timeout
        self._stopping = asyncio.Event()
        self._current: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        """Stop claiming new jobs; running jobs continue."""
        self._stopping.set()

    async def run(self, grace: float = 30.0) -> None:
        slots = [
            asyncio.create_task(self._slot(f"{self._id}/{i}")) for i in range(self._concurrency)
        ]
        reaper = asyncio.create_task(self._reaper())
        await self._stopping.wait()
        log.info("worker %s stopping; %d run(s) in flight", self._id, len(self._current))
        reaper.cancel()
        # Slots exit after their current job; give running jobs the grace period.
        pending: set[asyncio.Task[None]] = set()
        if slots:
            _, pending = await asyncio.wait(slots, timeout=grace)
        for t in pending:
            t.cancel()  # the executor marks interrupted runs on cancel
        for t in [*pending, reaper]:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _slot(self, consumer: str) -> None:
        while not self._stopping.is_set():
            try:
                lease = await self._claim(consumer)
            except Exception:
                log.exception("claim failed; retrying")
                await asyncio.sleep(1)
                continue
            if lease is None:
                continue
            log.info("run %s on %s (recovered=%s)", lease.job.run_id, consumer, lease.recovered)
            task = asyncio.create_task(self._executor.execute(lease))
            self._current.add(task)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise
            except Exception:
                log.exception("run %s crashed the executor", lease.job.run_id)
            finally:
                self._current.discard(task)

    async def _claim(self, consumer: str) -> Lease | None:
        # Stop waiting for work as soon as shutdown starts. A wake consumed by
        # a cancelled claim is re-issued by the reaper's recover().
        claim = asyncio.create_task(self._queue.claim(consumer, self._claim_timeout))
        stop = asyncio.create_task(self._stopping.wait())
        await asyncio.wait({claim, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not claim.done():
            claim.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await claim
            return None
        return claim.result()

    async def _reaper(self) -> None:
        while True:
            try:
                await self.reap_once()
            except Exception:
                log.exception("reaper pass failed")
            await asyncio.sleep(self._reaper_interval)

    async def reap_once(self) -> None:
        woken = await self._queue.recover()
        if woken:
            log.warning("woke %d agent(s) whose lock holder died", woken)
        cutoff = self._clock.now() - self._stale_after
        for run in await self._runs.stale(cutoff):
            done = await self._runs.transition(
                run.id,
                {RunStatus.RUNNING},
                RunStatus.INTERRUPTED,
                RunChanges(finished_at=self._clock.now(), error="worker heartbeat lost"),
            )
            if done is not None:
                log.warning("run %s interrupted: heartbeat lost", run.id)
                await self._executor.emit(done, EventKind.RUN_STATUS, status_event(done))
