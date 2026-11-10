"""BuilderToolkit on in-memory adapters with the fake runner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from playground_adapters.wiring import load_seed_folder
from playground_core.errors import (
    FailedPreconditionError,
    InvalidArgumentError,
    InvalidPathError,
    PermissionDeniedError,
)
from playground_core.models import ActorKind
from playground_core.services.builder import BuilderToolkit
from playground_core.services.credentials import CredentialInput
from playground_core.services.history import edit_lock
from playground_worker.main import BuilderTools, build_services

from .conftest import REPO_ROOT
from .runs_harness import FakeRunner, Platform, make_platform, memory_infra, running

BUILDER = "_builder"


@pytest.fixture
async def p(agents_root: Path) -> AsyncIterator[Platform]:
    platform = make_platform(memory_infra(agents_root), FakeRunner())
    await platform.agents.seed_system_agent(
        BUILDER, load_seed_folder(REPO_ROOT / "agents" / BUILDER)
    )
    await platform.credentials.create(
        CredentialInput(name="main", default_model="m"), "sk", make_default=True
    )
    await platform.credentials.create(
        CredentialInput(name="cheap", default_model="m2"), "sk2", make_default=False
    )
    yield platform


def kit(p: Platform, run_id: str = "builder-run") -> BuilderToolkit:
    return BuilderTools(p.infra, build_services(p.infra), BUILDER).toolkit(run_id)


async def test_seeded_builder_is_valid(p: Platform) -> None:
    report = await p.agents.validate(BUILDER)
    assert report.runnable and report.diagnostics == [], report.diagnostics
    assert [c.message for c in await p.infra.history.log(BUILDER, limit=5)] == ["Seed system agent"]


async def test_reads_and_listing(p: Platform) -> None:
    k = kit(p)
    created = await k.create_agent("notes", "Notes", "Keeps notes", "assistant")
    assert "CLAUDE.md" in created["files"] and created["diagnostics"] == []
    assert (await k.list_agents())["agents"] == [
        {"slug": "notes", "name": "Notes", "description": "Keeps notes"}
    ]
    names = {t["name"] for t in (await k.list_templates())["templates"]}
    assert names == {"blank", "assistant"}
    creds = (await k.list_credential_sets())["credential_sets"]
    assert [c["name"] for c in creds] == ["cheap", "main"]
    assert all("secret" not in json.dumps(c) for c in creds)
    agent = await k.get_agent("notes")
    assert "agent.json" in [f["path"] for f in agent["files"]]
    assert agent["recent_changes"][0]["actor"] == "builder"
    # The Builder may read itself, but not platform paths.
    assert "Builder" in (await k.read_agent_file(BUILDER, "CLAUDE.md"))["content"]
    with pytest.raises(InvalidPathError):
        await k.read_agent_file("notes", "../_builder/CLAUDE.md")


async def test_guardrails(p: Platform) -> None:
    k = kit(p)
    await k.create_agent("tool", "Tool", "")
    for call in (
        k.write_agent_file(BUILDER, "CLAUDE.md", "x"),
        k.delete_agent_file(BUILDER, "memory/MEMORY.md"),
        k.assign_credential_set(BUILDER, "cheap"),
        k.test_run_agent(BUILDER, "hi"),
    ):
        with pytest.raises(PermissionDeniedError, match="own folder"):
            await call
    with pytest.raises(PermissionDeniedError):
        await k.write_agent_file("tool", "state/session.json", "{}")
    with pytest.raises(PermissionDeniedError):
        await k.write_agent_file("tool", ".git/config", "")
    with pytest.raises(PermissionDeniedError):
        await k.delete_agent_file("tool", "agent.json")
    with pytest.raises(InvalidArgumentError):
        await k.assign_credential_set("tool", "nope")


async def test_writes_are_commits_tagged_with_the_run(p: Platform) -> None:
    k = kit(p, "run-42")
    await k.create_agent("tool", "Tool", "")
    out = await k.write_agent_file("tool", ".claude/skills/x/SKILL.md", "no frontmatter")
    assert out["diagnostics"][0]["severity"] == "warning"
    await k.assign_credential_set("tool", "cheap")
    manifest = json.loads((await k.read_agent_file("tool", "agent.json"))["content"])
    assert manifest["credential_set"] == "cheap"
    await k.assign_credential_set("tool", "")
    manifest = json.loads((await k.read_agent_file("tool", "agent.json"))["content"])
    assert "credential_set" not in manifest
    history = (await k.agent_history("tool"))["commits"]
    assert [c["message"] for c in history] == [
        "Update agent.json",
        "Update agent.json",
        "Update .claude/skills/x/SKILL.md",
        "Create agent from template 'blank'",
    ]
    assert {(c["actor"], c["run_id"]) for c in history} == {("builder", "run-42")}

    first = history[-1]["id"]
    back = await k.rollback_agent("tool", first)
    assert back["commit"]["message"].startswith("Roll back to ")
    assert (await k.validate_agent("tool"))["diagnostics"] == []
    log = await p.infra.history.log("tool", limit=1)
    assert log[0].actor is ActorKind.BUILDER


async def test_writes_wait_for_a_busy_agent(p: Platform) -> None:
    k = BuilderToolkit(
        agents=p.agents,
        files=build_services(p.infra).files,
        history=build_services(p.infra).history,
        credentials=p.credentials,
        conversations=p.conversations,
        events=p.infra.events,
        queue=p.infra.queue,
        builder_slug=BUILDER,
        run_id="r",
        lock_wait=0.3,
    )
    await k.create_agent("tool", "Tool", "")
    async with edit_lock(p.infra.queue, "tool", wait=1):  # stands in for a running turn
        with pytest.raises(FailedPreconditionError, match="is running"):
            await k.write_agent_file("tool", "CLAUDE.md", "x")


async def test_test_run_agent_waits_for_the_reply(p: Platform) -> None:
    k = kit(p)
    await k.create_agent("echo", "Echo", "")
    async with running(p.worker):
        out = await k.test_run_agent("echo", "hello there", timeout_seconds=10)
    assert out["status"] == "succeeded", out
    assert out["reply"] == "echo: hello there"
    assert out["tool_calls"] == ["Task"]
    conv = await p.conversations.get(out["conversation_id"])
    assert conv.conversation.title == "Builder test: hello there"


async def test_test_run_agent_returns_while_still_queued(p: Platform) -> None:
    k = kit(p)
    await k.create_agent("echo", "Echo", "")
    out = await k.test_run_agent("echo", "nobody runs this", timeout_seconds=0.1)
    assert out["status"] == "queued"
    assert "get_test_run" in out["note"]
    assert (await k.get_test_run(out["run_id"]))["status"] == "queued"
