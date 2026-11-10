"""The Builder end to end: the real Claude CLI (jailed where bwrap works)
drives the platform MCP tools in the worker, against a scripted fake model.

"Done when the Builder creates a working agent from a one-line description":
the script plays what a model would do for "an agent that writes a haiku
about any topic" and the new agent then answers its own test run.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from playground_adapters.wiring import load_seed_folder
from playground_core.events import EventKind
from playground_core.models import ActorKind, Provider, RunStatus
from playground_core.services.credentials import CredentialInput
from playground_worker.config import WorkerSettings
from playground_worker.jail import SandboxSettings
from playground_worker.launcher import bundled_cli
from playground_worker.main import BuilderTools, build_services, build_worker
from playground_worker.sdk_runner import ClaudeAgentRunner

from .conftest import REPO_ROOT
from .fake_model_api import SCRIPT_PREFIX, FakeModelAPI
from .runs_harness import memory_infra, running
from .test_sdk_runner import SANDBOX_MODE

try:
    bundled_cli()
except SystemExit:  # pragma: no cover - depends on the installed wheel
    pytest.skip("bundled Claude CLI not available", allow_module_level=True)

BUILDER = "_builder"
P = "mcp__platform__"
HAIKU_MD = """# Haiku Bot

Write one haiku (5-7-5 syllables) about the topic the user gives. Reply with the haiku only.

@memory/MEMORY.md
"""
SKILL_MD = """---
name: haiku
description: Write a haiku about a topic. Use when asked for a haiku or a short poem.
---

1. Pick one concrete image for the topic.
2. Write three lines of 5, 7 and 5 syllables.
"""


@dataclass
class World:
    services: Any
    infra: Any
    api: FakeModelAPI


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    infra = memory_infra(tmp_path / "agents")
    services = build_services(infra)
    seed = load_seed_folder(REPO_ROOT / "agents" / BUILDER)
    assert await services.agents.seed_system_agent(BUILDER, seed)
    with FakeModelAPI() as api:
        await services.credentials.create(
            CredentialInput(
                name="local", provider=Provider.GATEWAY, base_url=api.url, default_model="m"
            ),
            "sk-test",
            make_default=True,
        )
        # Unix socket paths are short; tmp_path can be too long for one.
        runtime = Path(tempfile.mkdtemp(prefix="ap-rt-"))
        sandbox = SandboxSettings(mode=SANDBOX_MODE, runtime_dir=runtime)
        runner = ClaudeAgentRunner(
            tmp_path / "agents",
            tmp_path / "sessions",
            ["NO_PROXY", "no_proxy"],
            sandbox,
            platform_tools=BuilderTools(infra, services, BUILDER),
        )
        settings = WorkerSettings(worker_id="w", worker_concurrency=2, heartbeat_seconds=1)
        async with running(build_worker(infra, settings, runner)):
            yield World(services, infra, api)


async def _wait(world: World, run_id: str, timeout: float = 240) -> RunStatus:
    async with asyncio.timeout(timeout):
        while True:
            run = await world.services.conversations.get_run(run_id)
            if run.status.terminal:
                return run.status
            await asyncio.sleep(0.1)


def _script(*steps: tuple[str, dict[str, Any]]) -> str:
    return SCRIPT_PREFIX + json.dumps([[P + name, args] for name, args in steps])


async def test_builder_creates_a_working_agent(world: World) -> None:
    slug = "haiku-bot"
    prompt = _script(
        ("list_templates", {}),
        (
            "create_agent",
            {"slug": slug, "name": "Haiku Bot", "description": "Writes a haiku about any topic"},
        ),
        ("write_agent_file", {"slug": slug, "path": "CLAUDE.md", "content": HAIKU_MD}),
        (
            "write_agent_file",
            {"slug": slug, "path": ".claude/skills/haiku/SKILL.md", "content": SKILL_MD},
        ),
        ("validate_agent", {"slug": slug}),
        ("test_run_agent", {"slug": slug, "prompt": "autumn rain", "timeout_seconds": 120}),
        ("write_agent_file", {"slug": BUILDER, "path": "CLAUDE.md", "content": "hijack"}),
        ("read_agent_file", {"slug": slug, "path": "state/x"}),
        ("diff_agent", {"slug": slug}),
    )
    s = world.services
    conv, run = await s.conversations.start_run(BUILDER, prompt)
    assert await _wait(world, run.id) is RunStatus.SUCCEEDED, (
        await s.conversations.get_run(run.id)
    ).error

    events = list(await world.infra.events.read(conv.id, 0, 10_000))
    calls = {e.data["tool_use_id"]: e.data["name"] for e in events if e.kind is EventKind.TOOL_CALL}
    results = {
        calls[e.data["tool_use_id"]]: e.data
        for e in events
        if e.kind is EventKind.TOOL_RESULT and e.data["tool_use_id"] in calls
    }
    assert [n.removeprefix(P) for n in calls.values()] == [
        "list_templates",
        "create_agent",
        "write_agent_file",
        "write_agent_file",
        "validate_agent",
        "test_run_agent",
        "write_agent_file",
        "read_agent_file",
        "diff_agent",
    ]

    # The new agent exists, is valid, and has what the Builder wrote.
    report = await s.agents.validate(slug)
    assert report.runnable and report.diagnostics == []
    assert (await s.files.read(slug, "CLAUDE.md")).data.decode() == HAIKU_MD

    # Its test run really ran the new agent and the Builder saw the reply.
    tested = json.loads(results[P + "test_run_agent"]["content"])
    assert tested["status"] == "succeeded", tested
    assert tested["reply"] == "pong: autumn rain"
    test_conv = await s.conversations.get(tested["conversation_id"])
    assert test_conv.conversation.agent_slug == slug
    assert test_conv.conversation.title.startswith("Builder test: ")

    # Guardrails: its own folder and platform paths are refused.
    own = results[P + "write_agent_file"]  # the last write_agent_file call
    assert own["is_error"] and "own folder" in own["content"]
    assert (await s.files.read(BUILDER, "CLAUDE.md")).data != b"hijack"
    assert results[P + "read_agent_file"]["is_error"]

    # Every change is a commit tagged with the Builder's run id; the test run
    # made its own (empty, so skipped) snapshot as the agent.
    history = await s.history.log(slug)
    assert [c.message for c in history] == [
        "Update .claude/skills/haiku/SKILL.md",
        "Update CLAUDE.md",
        "Create agent from template 'blank'",
    ]
    assert all(c.actor is ActorKind.BUILDER and c.run_id == run.id for c in history)
    diff = json.loads(results[P + "diff_agent"]["content"])
    assert [c["path"] for c in diff["changes"]] == [".claude/skills/haiku/SKILL.md"]
