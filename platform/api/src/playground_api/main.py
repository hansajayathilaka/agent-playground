"""Entry point: ``playground-api``."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from playground_api.app import build_container, build_server
from playground_api.config import Settings

log = logging.getLogger("playground_api")


async def serve(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        container = await build_container(settings, stack)
        server = await build_server(container)
        address = f"{settings.grpc_host}:{settings.grpc_port}"
        server.add_insecure_port(address)
        await server.start()
        log.info("API listening on %s", address)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("shutting down")
        await server.stop(grace=10)


def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve(settings))


if __name__ == "__main__":
    run()
