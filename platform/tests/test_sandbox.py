"""Sandbox escape suite: the real Claude CLI in the bubblewrap jail.

Each test asks the fake model for one Bash call (or uses an agent hook) that
tries to get out, then checks the tool output and the filesystem. Needs
bwrap with user namespaces; skipped where the jail can't start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from playground_core.events import EventKind
from playground_core.models import RunLimits, RunStatus
from playground_core.ports import RunRequest
from playground_worker.jail import SandboxSettings, SandboxUnavailableError, check_sandbox
from playground_worker.launcher import bundled_cli
from playground_worker.sdk_runner import ClaudeAgentRunner

from .fake_model_api import FakeModelAPI
from .test_sdk_runner import Sink

try:
    bundled_cli()
    check_sandbox(SandboxSettings())
except (SystemExit, SandboxUnavailableError) as exc:  # pragma: no cover - environment
    if os.environ.get("REQUIRE_SANDBOX"):
        raise
    pytest.skip(f"sandbox not available: {exc}", allow_module_level=True)

TEMPLATE = Path(__file__).resolve().parents[2] / "agents/templates/blank"


def _agent(root: Path, slug: str) -> Path:
    path = root / slug
    shutil.copytree(TEMPLATE, path)
    (path / "template.json").unlink(missing_ok=True)
    return path


@pytest.fixture
def api() -> Iterator[FakeModelAPI]:
    with FakeModelAPI() as server:
        yield server


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agents"
    _agent(root, "alpha")
    beta = _agent(root, "beta")
    (beta / "secret.txt").write_text("beta-secret")
    (beta / "state").mkdir()
    (beta / "state" / "session.json").write_text("beta-state")
    (beta / ".git").mkdir()
    (beta / ".git" / "config").write_text("beta-git")
    alpha = root / "alpha"
    (alpha / "state").mkdir()
    (alpha / "state" / "session.json").write_text("alpha-state")
    _agent(root, "_builder")
    (tmp_path / "worker-file.txt").write_text("worker-only")
    monkeypatch.setenv("PLAYGROUND_TEST_WORKER_SECRET", "do-not-leak")
    return tmp_path


def _all_cmdlines() -> str:
    out: list[str] = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            with contextlib.suppress(OSError):
                out.append((proc / "cmdline").read_bytes().decode(errors="replace"))
    return "\n".join(out)


def make_runner(world: Path, **overrides: object) -> ClaudeAgentRunner:
    settings = SandboxSettings(runtime_dir=world / "runtime", **overrides)  # pyright: ignore[reportArgumentType]
    return ClaudeAgentRunner(
        world / "agents", world / "sessions", ["NO_PROXY", "no_proxy"], settings
    )


@pytest.fixture
def runner(world: Path) -> ClaudeAgentRunner:
    return make_runner(world)


def request(
    api: FakeModelAPI,
    prompt: str,
    slug: str = "alpha",
    limits: RunLimits | None = None,
    allowlist: tuple[str, ...] = (),
) -> RunRequest:
    env = {"ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": "sk-fake-run-key"}
    return RunRequest(
        "r1",
        "c1",
        slug,
        prompt,
        "",
        "claude-test",
        8,
        None,
        env,
        limits=limits or RunLimits(),
        network_allowlist=allowlist,
    )


async def bash(
    runner: ClaudeAgentRunner, api: FakeModelAPI, command: str, slug: str = "alpha", **kw: object
) -> str:
    sink = Sink()
    out = await runner.run(request(api, f"run-bash: {command}", slug, **kw), sink, asyncio.Event())  # pyright: ignore[reportArgumentType]
    assert out.status is RunStatus.SUCCEEDED, out.error
    results = sink.of(EventKind.TOOL_RESULT)
    assert results, sink.events
    return str(results[0]["content"])


async def test_bash_cannot_read_outside_its_folder(
    runner: ClaudeAgentRunner, api: FakeModelAPI, world: Path
) -> None:
    out = await bash(
        runner,
        api,
        f"cat ../beta/secret.txt {world}/agents/beta/secret.txt {world}/worker-file.txt; "
        "ls /home /data /opt 2>&1; cat /etc/shadow; echo end",
    )
    assert "beta-secret" not in out
    assert "worker-only" not in out
    assert "end" in out


async def test_bash_writes_only_to_memory_and_workspace(
    runner: ClaudeAgentRunner, api: FakeModelAPI, world: Path
) -> None:
    agent = world / "agents" / "alpha"
    out = await bash(
        runner,
        api,
        "echo x > ../escaped.txt; echo x > CLAUDE.md; echo x > .claude/settings.json; "
        "echo x > tools/evil.sh; echo x > /tmp/scratch && echo tmp-ok; "
        "echo ok > workspace/w.txt; echo ok > memory/m.txt; echo end",
    )
    assert "end" in out and "tmp-ok" in out
    assert not (world / "agents" / "escaped.txt").exists()
    assert (agent / "CLAUDE.md").read_text() != "x\n"
    assert not (agent / "tools" / "evil.sh").exists()
    assert "x" not in (agent / ".claude" / "settings.json").read_text().splitlines()
    assert (agent / "workspace" / "w.txt").read_text() == "ok\n"
    assert (agent / "memory" / "m.txt").read_text() == "ok\n"


async def test_state_and_git_are_hidden(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    out = await bash(runner, api, "ls -A state .git 2>&1; cat state/session.json; echo end")
    assert "alpha-state" not in out
    assert "No such file" in out
    assert out.splitlines()[0] != "session.json"


async def test_processes_and_env_of_the_worker_are_invisible(
    runner: ClaudeAgentRunner, api: FakeModelAPI
) -> None:
    out = await bash(
        runner,
        api,
        "cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c PLAYGROUND_TEST_WORKER_SECRET;"
        " ls /proc | grep -c '^[0-9]'",
    )
    lines = out.split()
    assert lines[0] == "0"
    # bwrap's init, jail_init, the CLI and the shell: nothing of the worker.
    assert int(lines[1]) < 15
    assert os.getpid() > 0


async def test_no_direct_network(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    port = int(api.url.rsplit(":", 1)[1])
    script = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=3); print('direct-open')\n"
        "except OSError as e:\n"
        "    print('direct-blocked', e)\n"
    )
    out = await bash(runner, api, f"python3 -c {shlex.quote(script)}")
    assert "direct-blocked" in out, out
    assert "direct-open" not in out


async def test_proxy_blocks_private_addresses(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        script = (
            "import os, urllib.request\n"
            "try:\n"
            f"    urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=5); print('open')\n"
            "except Exception as e:\n"
            "    print('blocked', getattr(e, 'code', e))\n"
        )
        sink = Sink()
        req = request(api, f"run-bash: python3 -c {shlex.quote(script)}")
        out = await runner.run(req, sink, asyncio.Event())
        assert out.status is RunStatus.SUCCEEDED, out.error
        assert "blocked 403" in str(sink.of(EventKind.TOOL_RESULT)[0]["content"])
        notices = [str(n["message"]) for n in sink.of(EventKind.NOTICE)]
        assert any(f"127.0.0.1:{port}" in n for n in notices), notices
    finally:
        listener.close()


async def test_agent_hooks_run_inside_the_jail(
    runner: ClaudeAgentRunner, api: FakeModelAPI, world: Path
) -> None:
    agent = world / "agents" / "alpha"
    hook = (
        f"cat {world}/agents/beta/secret.txt > workspace/hook.txt 2>&1; "
        f"echo pwned > {world}/agents/beta/pwned.txt; echo ran >> workspace/hook.txt"
    )
    settings = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook}]}]
        }
    }
    (agent / ".claude" / "settings.json").write_text(json.dumps(settings))
    await bash(runner, api, "echo hi")
    log = (agent / "workspace" / "hook.txt").read_text()
    assert "ran" in log
    assert "beta-secret" not in log
    assert not (world / "agents" / "beta" / "pwned.txt").exists()


async def test_builder_reads_every_agent_but_not_their_state(
    runner: ClaudeAgentRunner, api: FakeModelAPI, world: Path
) -> None:
    beta = world / "agents" / "beta"
    out = await bash(
        runner,
        api,
        f"cat {beta}/secret.txt; cat {beta}/state/session.json {beta}/.git/config; "
        f"echo x > {beta}/written.txt; echo end",
        slug="_builder",
    )
    assert "beta-secret" in out
    assert "beta-state" not in out and "beta-git" not in out
    assert not (beta / "written.txt").exists()


async def test_background_processes_die_with_the_run(
    runner: ClaudeAgentRunner, api: FakeModelAPI
) -> None:
    marker = "4242.5"
    await bash(runner, api, f"(nohup sleep {marker} >/dev/null 2>&1 &); echo started")
    await asyncio.sleep(0.5)
    assert f"sleep\0{marker}" not in _all_cmdlines()


async def test_time_limit(runner: ClaudeAgentRunner, api: FakeModelAPI) -> None:
    sink = Sink()
    req = request(api, "slow please", limits=RunLimits(timeout_seconds=3))
    out = await asyncio.wait_for(runner.run(req, sink, asyncio.Event()), 60)
    assert out.status is RunStatus.FAILED
    assert "time limit" in out.error


async def test_symlinked_workspace_fails_the_run(
    runner: ClaudeAgentRunner, api: FakeModelAPI, world: Path
) -> None:
    agent = world / "agents" / "alpha"
    shutil.rmtree(agent / "workspace")
    (agent / "workspace").symlink_to(world / "agents" / "beta")
    out = await runner.run(request(api, "hello"), Sink(), asyncio.Event())
    assert out.status is RunStatus.FAILED
    assert "workspace/" in out.error
    assert not api.requests
