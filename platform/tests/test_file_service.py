from __future__ import annotations

import json
from pathlib import Path

import pytest

from playground_core.errors import (
    AlreadyExistsError,
    ConflictError,
    InvalidArgumentError,
    InvalidPathError,
    NotFoundError,
    PermissionDeniedError,
)
from playground_core.models import FileKind
from playground_core.services.agents import AgentService
from playground_core.services.files import MAX_WRITE_BYTES, FileService


async def test_tree_hides_platform_dirs(
    file_service: FileService, demo_agent: str, agents_root: Path
) -> None:
    (agents_root / demo_agent / "state").mkdir()
    (agents_root / demo_agent / "state" / "sessions.json").write_text("{}")
    paths = [e.path for e in await file_service.tree(demo_agent)]
    assert "agent.json" in paths and ".claude/settings.json" in paths
    assert not any(p.startswith("state") for p in paths)
    hidden = [e.path for e in await file_service.tree(demo_agent, include_hidden=True)]
    assert "state/sessions.json" in hidden
    # Directories first at each level.
    top = [p for p in paths if "/" not in p]
    kinds = {e.path: e.kind for e in await file_service.tree(demo_agent)}
    dirs_then_files = [kinds[p] for p in top]
    assert dirs_then_files == sorted(dirs_then_files, key=lambda k: k is not FileKind.DIRECTORY)


async def test_read_write_roundtrip_with_etags(file_service: FileService, demo_agent: str) -> None:
    out = await file_service.write(demo_agent, "workspace/notes.md", b"v1")
    assert out.diagnostics == []
    content = await file_service.read(demo_agent, "workspace/notes.md")
    assert content.data == b"v1" and content.etag == out.result.etag

    out2 = await file_service.write(demo_agent, "workspace/notes.md", b"v2", if_match=content.etag)
    with pytest.raises(ConflictError):
        await file_service.write(demo_agent, "workspace/notes.md", b"v3", if_match=content.etag)
    with pytest.raises(AlreadyExistsError):
        await file_service.write(demo_agent, "workspace/notes.md", b"x", create_only=True)
    assert (await file_service.read(demo_agent, "workspace/notes.md")).etag == out2.result.etag


async def test_writes_return_diagnostics_but_still_save(
    file_service: FileService, demo_agent: str
) -> None:
    out = await file_service.write(demo_agent, "agent.json", b'{"name": 1}')
    assert out.diagnostics and out.diagnostics[0].location == "/name"
    assert (await file_service.read(demo_agent, "agent.json")).data == b'{"name": 1}'


async def test_manifest_edit_updates_index(
    file_service: FileService, agent_service: AgentService, demo_agent: str
) -> None:
    await file_service.write(
        demo_agent, "agent.json", json.dumps({"name": "Renamed", "description": "d"}).encode()
    )
    (record,) = await agent_service.list()
    assert (record.name, record.description) == ("Renamed", "d")


@pytest.mark.parametrize("path", ["state/x.json", ".git/config", "./state/../state/y"])
async def test_platform_paths_are_read_only(
    file_service: FileService, demo_agent: str, path: str
) -> None:
    with pytest.raises((PermissionDeniedError, InvalidPathError)):
        await file_service.write(demo_agent, path, b"x")


async def test_agent_json_is_protected(file_service: FileService, demo_agent: str) -> None:
    with pytest.raises(PermissionDeniedError):
        await file_service.delete(demo_agent, "agent.json")
    with pytest.raises(PermissionDeniedError):
        await file_service.move(demo_agent, "agent.json", "old.json")


async def test_delete_move_mkdir(file_service: FileService, demo_agent: str) -> None:
    await file_service.mkdir(demo_agent, "docs/sub")
    with pytest.raises(AlreadyExistsError):
        await file_service.mkdir(demo_agent, "docs/sub")
    await file_service.write(demo_agent, "docs/sub/a.md", b"a")
    with pytest.raises(InvalidArgumentError):
        await file_service.delete(demo_agent, "docs")
    entry = await file_service.move(demo_agent, "docs/sub/a.md", "docs/b.md")
    assert entry.path == "docs/b.md"
    with pytest.raises(InvalidArgumentError):
        await file_service.move(demo_agent, "docs", "docs/inner")
    with pytest.raises(PermissionDeniedError):
        await file_service.move(demo_agent, "docs/b.md", "state/b.md")
    await file_service.delete(demo_agent, "docs", recursive=True)
    with pytest.raises(NotFoundError):
        await file_service.read(demo_agent, "docs/b.md")
    with pytest.raises(NotFoundError):
        await file_service.delete(demo_agent, "docs")


async def test_size_limit(file_service: FileService, demo_agent: str) -> None:
    with pytest.raises(InvalidArgumentError):
        await file_service.write(demo_agent, "big.bin", b"0" * (MAX_WRITE_BYTES + 1))


async def test_unknown_agent(file_service: FileService) -> None:
    with pytest.raises(NotFoundError):
        await file_service.tree("ghost")


async def test_traversal_through_service(file_service: FileService, demo_agent: str) -> None:
    with pytest.raises(InvalidPathError):
        await file_service.read(demo_agent, "../other/agent.json")
