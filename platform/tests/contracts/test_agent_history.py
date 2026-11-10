"""Contract: every AgentHistory adapter behaves the same.

The agents root is ``tmp_path / "agents"`` (the fixture's adapter uses it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground_core.errors import NotFoundError
from playground_core.models import Actor, ActorKind, ChangeKind
from playground_core.ports import AgentHistory


@pytest.fixture
def agent(tmp_path: Path) -> Path:
    folder = tmp_path / "agents" / "alpha"
    (folder / "memory").mkdir(parents=True)
    (folder / "agent.json").write_text('{"name": "Alpha"}\n')
    (folder / "CLAUDE.md").write_text("# Alpha\n")
    (folder / "state").mkdir()
    (folder / "state" / "session.json").write_text("secret-ish")
    return folder


async def test_commit_log_and_nothing_to_commit(agent_history: AgentHistory, agent: Path) -> None:
    assert await agent_history.log("alpha", limit=10) == []
    first = await agent_history.commit("alpha", "Create agent", Actor.user("admin"))
    assert first is not None
    assert first.message == "Create agent" and first.author == "admin"
    assert first.actor is ActorKind.USER and first.run_id == ""
    assert await agent_history.commit("alpha", "again", Actor.user("admin")) is None

    (agent / "CLAUDE.md").write_text("# Alpha\n\nBe brief.\n")
    second = await agent_history.commit("alpha", "Update CLAUDE.md", Actor.builder("run-1"))
    assert second is not None
    assert second.actor is ActorKind.BUILDER and second.run_id == "run-1"
    log = await agent_history.log("alpha", limit=10)
    assert [c.id for c in log] == [second.id, first.id]
    assert [c.id for c in await agent_history.log("alpha", limit=1)] == [second.id]
    only = await agent_history.log("alpha", limit=10, path="agent.json")
    assert [c.id for c in only] == [first.id]


async def test_state_is_never_recorded(agent_history: AgentHistory, agent: Path) -> None:
    await agent_history.commit("alpha", "start", Actor.platform())
    (agent / "state" / "session.json").write_text("changed")
    assert await agent_history.commit("alpha", "state only", Actor.platform()) is None
    changes = await agent_history.diff("alpha", "", "")
    assert all(not c.path.startswith("state/") for c in changes)


async def test_diff(agent_history: AgentHistory, agent: Path) -> None:
    first = await agent_history.commit("alpha", "start", Actor.platform())
    assert first is not None
    initial = await agent_history.diff("alpha", "", first.id)
    assert {c.path: c.change for c in initial} == {
        "agent.json": ChangeKind.ADDED,
        "CLAUDE.md": ChangeKind.ADDED,
    }
    (agent / "CLAUDE.md").write_text("# Alpha\nline two\n")
    (agent / "agent.json").unlink()
    (agent / "memory" / "notes.md").write_text("note\n")
    (agent / "blob.bin").write_bytes(b"\x00\x01\x02" * 10)
    second = await agent_history.commit("alpha", "edit", Actor.agent("alpha", "r1"))
    assert second is not None
    changes = {c.path: c for c in await agent_history.diff("alpha", "", "")}
    assert changes["CLAUDE.md"].change is ChangeKind.MODIFIED
    assert "+line two" in changes["CLAUDE.md"].patch
    assert changes["CLAUDE.md"].additions == 1
    assert changes["agent.json"].change is ChangeKind.DELETED
    assert changes["memory/notes.md"].change is ChangeKind.ADDED
    assert changes["blob.bin"].binary and changes["blob.bin"].patch == ""
    # Explicit range equals the default (parent of the latest commit).
    explicit = await agent_history.diff("alpha", first.id, second.id)
    assert {c.path for c in explicit} == set(changes)
    with pytest.raises(NotFoundError):
        await agent_history.diff("alpha", "", "0" * 40)


async def test_rename_is_one_change(agent_history: AgentHistory, agent: Path) -> None:
    (agent / "memory" / "long.md").write_text("".join(f"line {i}\n" for i in range(50)))
    await agent_history.commit("alpha", "start", Actor.platform())
    (agent / "memory" / "long.md").rename(agent / "memory" / "moved.md")
    await agent_history.commit("alpha", "move", Actor.platform())
    changes = await agent_history.diff("alpha", "", "")
    assert len(changes) == 1
    assert changes[0].change is ChangeKind.RENAMED
    assert (changes[0].old_path, changes[0].path) == ("memory/long.md", "memory/moved.md")


async def test_restore_whole_folder_and_one_file(agent_history: AgentHistory, agent: Path) -> None:
    first = await agent_history.commit("alpha", "start", Actor.platform())
    assert first is not None
    (agent / "CLAUDE.md").write_text("changed\n")
    (agent / "memory" / "new.md").write_text("added later\n")
    (agent / "agent.json").write_text('{"name": "Renamed"}\n')
    await agent_history.commit("alpha", "edit", Actor.platform())

    await agent_history.restore("alpha", first.id, "CLAUDE.md")
    assert (agent / "CLAUDE.md").read_text() == "# Alpha\n"
    assert (agent / "memory" / "new.md").exists()  # only that file

    await agent_history.restore("alpha", first.id)
    assert not (agent / "memory" / "new.md").exists()  # added since: removed
    assert (agent / "agent.json").read_text() == '{"name": "Alpha"}\n'
    assert (agent / "state" / "session.json").read_text() == "secret-ish"  # untouched
    back = await agent_history.commit("alpha", "rollback", Actor.user("admin"))
    assert back is not None
    assert await agent_history.diff("alpha", first.id, back.id) == []
    with pytest.raises(NotFoundError):
        await agent_history.restore("alpha", first.id, "never/there.md")
    with pytest.raises(NotFoundError):
        await agent_history.restore("alpha", "f" * 40)


async def test_symlinks_are_not_checked_out(agent_history: AgentHistory, agent: Path) -> None:
    # A symlink the agent planted in workspace/ is recorded, but a restore
    # writes a plain file, never a link that could point outside the folder.
    (agent / "workspace").mkdir()
    (agent / "workspace" / "link").symlink_to("/etc/passwd")
    first = await agent_history.commit("alpha", "start", Actor.platform())
    assert first is not None
    (agent / "workspace" / "link").unlink()
    await agent_history.commit("alpha", "drop", Actor.platform())
    await agent_history.restore("alpha", first.id)
    restored = agent / "workspace" / "link"
    assert restored.exists() and not restored.is_symlink()
