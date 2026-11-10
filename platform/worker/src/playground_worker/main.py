"""Entry point: ``playground-worker``."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta

from claude_agent_sdk.types import McpServerConfig

from playground_adapters.wiring import Infra, build_infra
from playground_core.ports import AgentRunner, RunRequest
from playground_core.services.agents import AgentService
from playground_core.services.builder import BuilderToolkit
from playground_core.services.conversations import ConversationService
from playground_core.services.credentials import CredentialService
from playground_core.services.execution import LeaseTiming, RunExecutor
from playground_core.services.files import FileService
from playground_core.services.history import HistoryService
from playground_worker.builder_mcp import SERVER_NAME, builder_server
from playground_worker.config import WorkerSettings
from playground_worker.jail import check_sandbox
from playground_worker.worker import Worker

log = logging.getLogger("playground_worker")


@dataclass(frozen=True, slots=True)
class WorkerServices:
    agents: AgentService
    files: FileService
    history: HistoryService
    credentials: CredentialService
    conversations: ConversationService


def build_services(infra: Infra) -> WorkerServices:
    agents = AgentService(infra.store, infra.agents, infra.templates, infra.clock, infra.history)
    credentials = CredentialService(infra.credentials, infra.secrets, infra.store, infra.clock)
    conversations = ConversationService(
        infra.conversations,
        infra.runs,
        agents,
        infra.store,
        credentials,
        infra.queue,
        infra.events,
        infra.control,
        infra.clock,
    )
    return WorkerServices(
        agents=agents,
        files=FileService(infra.store, agents, infra.history),
        history=HistoryService(infra.history, agents, infra.queue),
        credentials=credentials,
        conversations=conversations,
    )


class BuilderTools:
    """Adds the platform MCP server to Builder runs (and nothing to others)."""

    def __init__(self, infra: Infra, services: WorkerServices, builder_slug: str) -> None:
        self._infra = infra
        self._s = services
        self._builder_slug = builder_slug

    def toolkit(self, run_id: str) -> BuilderToolkit:
        s = self._s
        return BuilderToolkit(
            agents=s.agents,
            files=s.files,
            history=s.history,
            credentials=s.credentials,
            conversations=s.conversations,
            events=self._infra.events,
            queue=self._infra.queue,
            builder_slug=self._builder_slug,
            run_id=run_id,
        )

    def __call__(self, request: RunRequest) -> Mapping[str, McpServerConfig]:
        if request.agent_slug != self._builder_slug:
            return {}
        return {SERVER_NAME: builder_server(self.toolkit(request.run_id))}


def build_worker(infra: Infra, settings: WorkerSettings, runner: AgentRunner) -> Worker:
    services = build_services(infra)
    agents, credentials = services.agents, services.credentials
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
        history=infra.history,
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

    sandbox = settings.sandbox()
    check_sandbox(sandbox)
    if sandbox.mode == "none":
        log.warning("SANDBOX_MODE=none: agents run unconfined (development only)")
    async with AsyncExitStack() as stack:
        infra = await build_infra(settings, stack)
        tools = BuilderTools(infra, build_services(infra), settings.builder_slug)
        runner = ClaudeAgentRunner(
            settings.agents_root,
            settings.sessions_root,
            settings.run_env_passthrough,
            sandbox,
            platform_tools=tools,
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
