from __future__ import annotations

import json
from pathlib import Path

import pytest

from playground_adapters.fs_store import LocalAgentStore
from playground_adapters.memory import InMemoryAgentRepository
from playground_adapters.templates import FsTemplateCatalog
from playground_core.errors import AlreadyExistsError, InvalidArgumentError, NotFoundError
from playground_core.services.agents import AgentService

from .conftest import TEMPLATES, FixedClock


async def test_templates_are_listed(agent_service: AgentService) -> None:
    names = [t.name for t in await agent_service.list_templates()]
    assert names == ["assistant", "blank"]


async def test_create_renders_template(agent_service: AgentService, agents_root: Path) -> None:
    record = await agent_service.create("writer", 'Writer "W"', "Writes things", "assistant")
    assert record.slug == "writer" and record.template == "assistant"

    folder = agents_root / "writer"
    manifest = json.loads((folder / "agent.json").read_text())
    assert manifest["name"] == 'Writer "W"'
    assert manifest["tool_packs"] == ["web"]
    assert (folder / "CLAUDE.md").read_text().startswith('# Writer "W"')
    assert (folder / ".claude/skills/research/SKILL.md").exists()
    assert not (folder / "template.json").exists()

    details = await agent_service.get("writer")
    assert details.diagnostics == []
    assert [a.slug for a in await agent_service.list()] == ["writer"]


async def test_create_defaults(agent_service: AgentService) -> None:
    record = await agent_service.create("plain", "", "")
    assert record.name == "plain" and record.template == "blank"


async def test_create_rejects_duplicates_and_bad_input(agent_service: AgentService) -> None:
    await agent_service.create("dup", "Dup", "")
    with pytest.raises(AlreadyExistsError):
        await agent_service.create("dup", "Dup", "")
    with pytest.raises(InvalidArgumentError):
        await agent_service.create("Bad Slug", "x", "")
    with pytest.raises(InvalidArgumentError):
        await agent_service.create("_builder", "x", "")
    with pytest.raises(NotFoundError):
        await agent_service.create("ok", "x", "", "no-such-template")


async def test_delete_moves_folder_to_trash(agent_service: AgentService, agents_root: Path) -> None:
    await agent_service.create("gone", "Gone", "")
    await agent_service.delete("gone")
    assert not (agents_root / "gone").exists()
    assert any(p.name.startswith("gone-") for p in (agents_root / ".trash").iterdir())
    assert await agent_service.list() == []
    with pytest.raises(NotFoundError):
        await agent_service.get("gone")
    # The slug can be reused after delete.
    await agent_service.create("gone", "Gone again", "")


async def test_validate_flags_broken_manifest(
    agent_service: AgentService, demo_agent: str, agents_root: Path
) -> None:
    (agents_root / demo_agent / "agent.json").write_text('{"name": ""}')
    report = await agent_service.validate(demo_agent)
    assert not report.runnable
    (agents_root / demo_agent / "agent.json").unlink()
    report = await agent_service.validate(demo_agent)
    assert [d.message for d in report.diagnostics] == ["agent.json is missing"]


async def test_reconcile_indexes_existing_folders(agents_root: Path, clock: FixedClock) -> None:
    (agents_root / "_builder").mkdir()
    (agents_root / "_builder" / "agent.json").write_text('{"name": "Builder"}')
    (agents_root / "stray").mkdir()
    (agents_root / "Not A Slug").mkdir()
    repo = InMemoryAgentRepository()
    service = AgentService(LocalAgentStore(agents_root), repo, FsTemplateCatalog(TEMPLATES), clock)
    await service.create("kept", "Kept", "")
    import shutil

    shutil.rmtree(agents_root / "kept")
    await service.reconcile()
    assert [(r.slug, r.name) for r in await repo.list()] == [
        ("_builder", "Builder"),
        ("stray", "stray"),
    ]
