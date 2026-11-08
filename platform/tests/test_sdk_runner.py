"""The real Claude CLI (bundled with claude-agent-sdk) driven by
ClaudeAgentRunner against a fake Messages API on localhost.

Covers what fakes can't: SDK options, the env-scrubbing launcher, the guard
hook, session resume across runs and cancel mid-stream.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest

from playground_core.events import EventKind
from playground_core.models import RunStatus
from playground_core.ports import RunRequest
from playground_worker.jail import SandboxSettings, SandboxUnavailableError, check_sandbox
from playground_worker.launcher import bundled_cli
from playground_worker.sdk_runner import ClaudeAgentRunner

from .fake_model_api import FakeModelAPI

try:
    bundled_cli()
except SystemExit:  # pragma: no cover - depends on the installed wheel
    pytest.skip("bundled Claude CLI not available", allow_module_level=True)


# Jailed where bwrap works (CI, devcontainer, worker image), plain otherwise.
def _sandbox_mode() -> Literal["bwrap", "none"]:
    try:
        check_sandbox(SandboxSettings())
    except SandboxUnavailableError:  # pragma: no cover - environment
        return "none"
    return "bwrap"


SANDBOX_MODE = _sandbox_mode()


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[EventKind, Mapping[str, Any], str]] = []

    async def __call__(
        self, kind: EventKind, data: Mapping[str, Any], *, parent_tool_use_id: str = ""
    ) -> None:
        self.events.append((kind, data, parent_tool_use_id))

    def of(self, kind: EventKind) -> list[Mapping[str, Any]]:
        return [d for k, d, _ in self.events if k is kind]


@pytest.fixture
def api() -> Iterator[FakeModelAPI]:
    with FakeModelAPI() as server:
        yield server


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeAgentRunner:
    root = tmp_path / "agents"
    agent = root / "alpha"
    shutil.copytree(Path(__file__).resolve().parents[2] / "agents/templates/blank", agent)
    (agent / "template.json").unlink(missing_ok=True)
    # A worker secret that must never reach the agent's processes.
    monkeypatch.setenv("PLAYGROUND_TEST_WORKER_SECRET", "do-not-leak")
    sandbox = SandboxSettings(mode=SANDBOX_MODE, runtime_dir=tmp_path / "runtime")
    return ClaudeAgentRunner(root, tmp_path / "sessions", ["NO_PROXY", "no_proxy"], sandbox)


def request(api: FakeModelAPI, prompt: str, resume: str = "", run_id: str = "r1") -> RunRequest:
    env = {"ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": "sk-fake-run-key"}
    return RunRequest(run_id, "c1", "alpha", prompt, resume, "claude-test", 8, None, env)


async def test_turn_streams_and_resumes(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    sink = Sink()
    first = await runner.run(request(api, "hello use-tool"), sink, asyncio.Event())
    assert first.status is RunStatus.SUCCEEDED, first.error
    assert first.session_id
    assert [d["name"] for d in sink.of(EventKind.TOOL_CALL)] == ["Write"]
    assert sink.of(EventKind.TOOL_RESULT)[0]["is_error"] is False
    assert sink.of(EventKind.TEXT_DELTA)
    assert sink.of(EventKind.ASSISTANT_TEXT)[-1]["text"] == "done"
    assert sink.of(EventKind.RESULT)[-1]["num_turns"] == 2
    assert (runner_agent(runner) / "workspace" / "out.txt").read_text() == "written"
    assert api.headers[0]["x-api-key"] == "sk-fake-run-key"

    sink2 = Sink()
    second = await runner.run(
        request(api, "second", resume=first.session_id, run_id="r2"), sink2, asyncio.Event()
    )
    assert second.status is RunStatus.SUCCEEDED, second.error
    assert second.session_id == first.session_id
    assert sink2.of(EventKind.ASSISTANT_TEXT)[-1]["text"] == "pong: second"
    # The resumed request carries the whole earlier conversation.
    history = str(api.agent_requests()[-1]["messages"])
    assert "hello use-tool" in history and "second" in history


async def test_guard_denies_writes_outside_the_folder(
    runner: ClaudeAgentRunner, api: FakeModelAPI, tmp_path: Path
) -> None:
    sink = Sink()
    out = await runner.run(request(api, "please use-escape"), sink, asyncio.Event())
    assert out.status is RunStatus.SUCCEEDED, out.error
    result = sink.of(EventKind.TOOL_RESULT)[0]
    assert result["is_error"] is True
    assert "outside this agent's folder" in result["content"]
    assert not (tmp_path / "agents" / "escaped.txt").exists()


async def test_agent_processes_get_only_the_run_env(
    runner: ClaudeAgentRunner, api: FakeModelAPI, tmp_path: Path
) -> None:
    sink = Sink()
    out = await runner.run(request(api, "use-bash"), sink, asyncio.Event())
    assert out.status is RunStatus.SUCCEEDED, out.error
    env_dump = (runner_agent(runner) / "workspace" / "env.txt").read_text()
    names = {line.split("=", 1)[0] for line in env_dump.splitlines() if "=" in line}
    assert "PLAYGROUND_TEST_WORKER_SECRET" not in names
    assert "do-not-leak" not in env_dump
    assert "PLAYGROUND_DROP_ENV" not in names
    assert f"CLAUDE_CONFIG_DIR={tmp_path / 'sessions' / 'alpha' / '.claude'}" in env_dump


async def test_cancel_mid_stream(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    sink = Sink()
    cancel = asyncio.Event()
    task = asyncio.create_task(runner.run(request(api, "slow please"), sink, cancel))
    async with asyncio.timeout(60):
        while not api.requests:
            await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    cancel.set()
    out = await asyncio.wait_for(task, 30)
    assert out.status is RunStatus.CANCELLED


def runner_agent(runner: ClaudeAgentRunner) -> Path:
    return runner._agents_root / "alpha"  # pyright: ignore[reportPrivateUsage]
