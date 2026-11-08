"""Contract: every AgentStore adapter behaves the same."""

from __future__ import annotations

import pytest

from playground_core.errors import (
    AlreadyExistsError,
    ConflictError,
    InvalidArgumentError,
    InvalidPathError,
    NotFoundError,
)
from playground_core.models import FileKind
from playground_core.ports import AgentStore

FILES = {"agent.json": b'{"name": "x"}', "workspace/a.txt": b"a", "state/s.json": b"{}"}


@pytest.fixture
async def store(agent_store: AgentStore) -> AgentStore:
    await agent_store.create_agent("one", FILES)
    return agent_store


async def test_create_and_list(store: AgentStore) -> None:
    assert await store.exists("one")
    assert not await store.exists("two")
    await store.create_agent("two", {"agent.json": b"{}"})
    assert list(await store.list_agent_slugs()) == ["one", "two"]
    with pytest.raises(AlreadyExistsError):
        await store.create_agent("one", {})


async def test_create_rejects_unsafe_template_paths(agent_store: AgentStore) -> None:
    with pytest.raises(InvalidPathError):
        await agent_store.create_agent("bad", {"../escape.txt": b"x"})
    assert not await agent_store.exists("bad")


async def test_trash(store: AgentStore) -> None:
    await store.trash_agent("one")
    assert not await store.exists("one")
    assert list(await store.list_agent_slugs()) == []
    with pytest.raises(NotFoundError):
        await store.read("one", "agent.json")


async def test_tree(store: AgentStore) -> None:
    visible = {e.path: e.kind for e in await store.tree("one", include_hidden=False)}
    assert visible == {
        "workspace": FileKind.DIRECTORY,
        "workspace/a.txt": FileKind.FILE,
        "agent.json": FileKind.FILE,
    }
    hidden = {e.path for e in await store.tree("one", include_hidden=True)}
    assert {"state", "state/s.json"} <= hidden


async def test_read_write_etag(store: AgentStore) -> None:
    first = await store.read("one", "workspace/a.txt")
    assert first.data == b"a" and first.entry.size == 1
    w = await store.write("one", "workspace/a.txt", b"bb", if_match=first.etag)
    assert w.entry.size == 2 and w.etag != first.etag
    with pytest.raises(ConflictError):
        await store.write("one", "workspace/a.txt", b"c", if_match=first.etag)
    with pytest.raises(ConflictError):
        await store.write("one", "workspace/new.txt", b"c", if_match=first.etag)
    with pytest.raises(AlreadyExistsError):
        await store.write("one", "workspace/a.txt", b"c", create_only=True)
    await store.write("one", "deep/er/file.txt", b"x")  # parents created
    assert (await store.read("one", "deep/er/file.txt")).data == b"x"


async def test_directories(store: AgentStore) -> None:
    with pytest.raises(InvalidArgumentError):
        await store.read("one", "workspace")
    with pytest.raises(InvalidArgumentError):
        await store.write("one", "workspace", b"x")
    entry = await store.mkdir("one", "x/y")
    assert entry.kind is FileKind.DIRECTORY and entry.path == "x/y"
    with pytest.raises(InvalidArgumentError):
        await store.delete("one", "workspace", recursive=False)
    await store.delete("one", "x/y", recursive=False)
    await store.delete("one", "workspace", recursive=True)
    with pytest.raises(NotFoundError):
        await store.delete("one", "workspace", recursive=True)


async def test_move(store: AgentStore) -> None:
    entry = await store.move("one", "workspace/a.txt", "archive/a.txt")
    assert entry.path == "archive/a.txt"
    with pytest.raises(NotFoundError):
        await store.move("one", "workspace/a.txt", "b.txt")
    await store.write("one", "b.txt", b"b")
    with pytest.raises(AlreadyExistsError):
        await store.move("one", "archive/a.txt", "b.txt")
    with pytest.raises(InvalidArgumentError):
        await store.move("one", "archive", "archive/inner")


async def test_paths_are_guarded(store: AgentStore) -> None:
    await store.create_agent("other", {"secret": b"s"})
    for bad in ["../other/secret", "/etc/passwd", "workspace/../../other/secret"]:
        with pytest.raises(InvalidPathError):
            await store.read("one", bad)
        with pytest.raises(InvalidPathError):
            await store.write("one", bad, b"x")
