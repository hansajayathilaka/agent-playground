"""Entry point: ``playground-worker``."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from datetime import timedelta

from playground_adapters.wiring import Infra, build_infra
from playground_core.ports import AgentRunner
from playground_core.services.agents import AgentService
from playground_core.services.credentials import CredentialService
from playground_core.services.execution import LeaseTiming, RunExecutor
from playground_worker.config import WorkerSettings
from playground_worker.worker import Worker

log = logging.getLogger("playground_worker")


def build_worker(infra: Infra, settings: WorkerSettings, runner: AgentRunner) -> Worker:
    agents = AgentService(infra.store, infra.agents, infra.templates, infra.clock)
    credentials = CredentialService(infra.credentials, infra.secrets, infra.store, infra.clock)
    executor = RunExecutor(
        runs=infra.runs,
        conversations=infra.conversations,
        agents=agents,
        store=infra.store,
        credentials=credentials,
        queue=infra.queue,
        events=infra.events,
        control=infra.control,
        runner=runner,
        clock=infra.clock,
        worker_id=settings.worker_id,
        timing=LeaseTiming(
            ttl=timedelta(seconds=settings.lease_ttl_seconds),
            heartbeat=timedelta(seconds=settings.heartbeat_seconds),
        ),
    )
    return Worker(
        executor=executor,
        queue=infra.queue,
        runs=infra.runs,
        clock=infra.clock,
        worker_id=settings.worker_id,
        concurrency=settings.worker_concurrency,
        stale_after=timedelta(seconds=settings.stale_after_seconds),
        reaper_interval=settings.reaper_interval_seconds,
    )


async def serve(settings: WorkerSettings) -> None:
    from playground_worker.sdk_runner import ClaudeAgentRunner

    async with AsyncExitStack() as stack:
        infra = await build_infra(settings, stack)
        runner = ClaudeAgentRunner(
            settings.agents_root, settings.sessions_root, settings.run_env_passthrough
        )
        worker = build_worker(infra, settings, runner)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
        log.info(
            "worker %s started with %d slot(s)", settings.worker_id, settings.worker_concurrency
        )
        await worker.run(grace=settings.shutdown_grace_seconds)
        log.info("worker %s stopped", settings.worker_id)


def run() -> None:
    settings = WorkerSettings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve(settings))


if __name__ == "__main__":
    run()
