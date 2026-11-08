from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from playground_adapters.auth import StaticAdminAuthProvider
from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import InMemoryAgentRepository, InMemorySessionStore
from playground_adapters.templates import FsTemplateCatalog
from playground_core.services.agents import AgentService
from playground_core.services.auth import AuthService
from playground_core.services.files import FileService

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "agents" / "templates"

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct horse battery staple"


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def agents_root(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    return root


@pytest.fixture
def store(agents_root: Path) -> LocalAgentStore:
    return LocalAgentStore(agents_root)


@pytest.fixture
def agent_service(store: LocalAgentStore, clock: FixedClock) -> AgentService:
    return AgentService(store, InMemoryAgentRepository(), FsTemplateCatalog(TEMPLATES), clock)


@pytest.fixture
def file_service(store: LocalAgentStore, agent_service: AgentService) -> FileService:
    return FileService(store, agent_service)


@pytest.fixture
def auth_service(clock: FixedClock) -> AuthService:
    return AuthService(
        StaticAdminAuthProvider(ADMIN_USER, ADMIN_PASSWORD),
        InMemorySessionStore(),
        clock,
        timedelta(hours=1),
    )


def env_or_skip(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture
async def demo_agent(agent_service: AgentService) -> AsyncIterator[str]:
    await agent_service.create("demo", "Demo", "A demo agent", "blank")
    yield "demo"
