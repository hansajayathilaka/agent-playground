"""Unit tests for the jail pieces that don't need bwrap: argv building,
egress policy and proxy, cgroup bookkeeping, launcher env and builder guard."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from playground_worker.cgroups import CgroupLimits, RunCgroups
from playground_worker.egress import Denied, EgressPolicy, EgressProxy, Upstream, host_matches
from playground_worker.guard import check_tool
from playground_worker.jail import JAIL_VAR, JailError, JailSpec, bwrap_args
from playground_worker.launcher import DROP_VAR, scrubbed_env


def _spec(tmp_path: Path, **kw: object) -> JailSpec:
    agent = tmp_path / "agents" / "alpha"
    for d in ("memory", "workspace", "state"):
        (agent / d).mkdir(parents=True, exist_ok=True)
    sessions = tmp_path / "sessions" / "alpha"
    sessions.mkdir(parents=True, exist_ok=True)
    base: dict[str, object] = {
        "agent_dir": str(agent),
        "session_dir": str(sessions),
        "cli_dir": str(tmp_path),
        "egress_socket": str(tmp_path / "e.sock"),
        "python": "/usr/bin/python3",
        "jail_init": str(tmp_path / "jail_init.py"),
    }
    base.update(kw)
    return JailSpec(**base)  # pyright: ignore[reportArgumentType]


def test_bwrap_args_bind_layout(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    args = bwrap_args(spec, ["claude", "--x"])
    agent = spec.agent_dir
    joined = " ".join(args)
    assert f"--ro-bind {agent} {agent}" in joined
    assert f"--bind {agent}/memory {agent}/memory" in joined
    assert f"--bind {agent}/workspace {agent}/workspace" in joined
    assert f"--tmpfs {agent}/state" in joined
    assert "--unshare-net" in args and "--die-with-parent" in args
    # The command runs under jail_init, after the separator.
    tail = args[args.index("--") + 1 :]
    assert tail[0] == "/usr/bin/python3" and tail[-2:] == ["claude", "--x"]
    # The worker's data and home dirs are never bound.
    assert "/home" not in args and "/data" not in args


def test_bwrap_args_host_network(tmp_path: Path) -> None:
    args = bwrap_args(_spec(tmp_path, network="host"), ["claude"])
    assert "--unshare-net" not in args
    assert args[args.index("--") + 1 :] == ["claude"]


def test_symlinked_writable_dir_is_refused(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    ws = Path(spec.agent_dir) / "workspace"
    ws.rmdir()
    ws.symlink_to(tmp_path)
    with pytest.raises(JailError):
        bwrap_args(spec, ["claude"])


def test_spec_round_trips_json(tmp_path: Path) -> None:
    spec = _spec(tmp_path, ro_files=("/a",), rlimits={"core": 0})
    assert JailSpec.from_json(spec.to_json()) == spec


def test_launcher_scrubs_platform_vars() -> None:
    env = {"KEEP": "1", "SECRET": "x", DROP_VAR: "SECRET", JAIL_VAR: "{}"}
    assert scrubbed_env(env) == {"KEEP": "1"}


@pytest.mark.parametrize(
    ("host", "pattern", "ok"),
    [
        ("example.com", "example.com", True),
        ("api.example.com", "example.com", False),
        ("api.example.com", "*.example.com", True),
        ("example.com", ".example.com", True),
        ("evilexample.com", ".example.com", False),
        ("EXAMPLE.com.", "example.com", True),
    ],
)
def test_host_matches(host: str, pattern: str, ok: bool) -> None:
    assert host_matches(host, pattern) is ok


def test_policy_open_internet_blocks_private() -> None:
    policy = EgressPolicy(endpoints=frozenset({("127.0.0.1", 8000)}))
    policy.check_name("127.0.0.1", 8000)  # the model endpoint
    policy.check_addresses("127.0.0.1", 8000, ["127.0.0.1"])
    for host in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "localhost", "::1"):
        with pytest.raises(Denied):
            policy.check_name(host, 80)
    policy.check_name("example.com", 443)
    for ip in ("10.0.0.5", "172.18.0.2", "::ffff:127.0.0.1", "100.64.1.1"):
        with pytest.raises(Denied):
            policy.check_addresses("redis", 6379, [ip])
    policy.check_addresses("example.com", 443, ["93.184.215.14"])


def test_policy_allowlist() -> None:
    policy = EgressPolicy(
        endpoints=frozenset({("api.anthropic.com", 443)}), allowlist=("*.github.com",)
    )
    policy.check_name("api.github.com", 443)
    policy.check_name("api.anthropic.com", 443)
    with pytest.raises(Denied, match="allowlist"):
        policy.check_name("example.com", 443)


def test_upstream_no_proxy() -> None:
    up = Upstream(
        {
            "HTTPS_PROXY": "http://proxy.local:3128",
            "NO_PROXY": "localhost,127.0.0.0/8,.internal,*.svc",
        }
    )
    assert up.proxy_for("example.com") == ("proxy.local", 3128)
    assert up.proxy_for("127.0.0.1") is None
    assert up.proxy_for("a.internal") is None
    assert up.proxy_for("x.svc") is None
    assert Upstream({}).proxy_for("example.com") is None


@contextlib.asynccontextmanager
async def _http_server(body: bytes) -> AsyncGenerator[int]:
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()


async def _through(sock: Path, request: bytes) -> bytes:
    r, w = await asyncio.open_unix_connection(str(sock))
    w.write(request)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 10)
    w.close()
    return data


async def test_egress_proxy_endpoint_and_denial(tmp_path: Path) -> None:
    denied: list[str] = []

    async def on_deny(host: str, port: int, reason: str) -> None:
        denied.append(f"{host}:{port}")

    async with _http_server(b"model-ok") as port:
        policy = EgressPolicy(endpoints=frozenset({("127.0.0.1", port)}))
        sock = tmp_path / "e.sock"
        async with EgressProxy(sock, policy, Upstream({}), on_deny):
            ok = await _through(
                sock, f"GET http://127.0.0.1:{port}/v1 HTTP/1.1\r\nHost: x\r\n\r\n".encode()
            )
            assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"model-ok")
            tunnel = await _through(
                sock,
                f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\n\r\n".encode(),
            )
            assert tunnel.startswith(b"HTTP/1.1 200 Connection Established")
            assert tunnel.endswith(b"model-ok")
            blocked = await _through(sock, b"CONNECT 127.0.0.1:6379 HTTP/1.1\r\n\r\n")
            assert blocked.startswith(b"HTTP/1.1 403")
        assert not sock.exists()
    assert denied == ["127.0.0.1:6379"]


def test_cgroups_unavailable_without_root(tmp_path: Path) -> None:
    assert not RunCgroups(None).available
    assert not RunCgroups(tmp_path / "missing").available
    (tmp_path / "cgroup.subtree_control").write_text("memory pids\n")
    assert "cpu" in RunCgroups(tmp_path).reason


def test_cgroups_write_limits(tmp_path: Path) -> None:
    (tmp_path / "cgroup.subtree_control").write_text("cpu memory pids\n")
    cg = RunCgroups(tmp_path)
    assert cg.available
    path = cg.create("run/../1", CgroupLimits(memory_mb=512, cpus=1.5, max_processes=64))
    assert path is not None and path.parent == tmp_path and path.name == "run-run1"
    assert (path / "memory.max").read_text() == str(512 * 1024 * 1024)
    assert (path / "pids.max").read_text() == "64"
    assert (path / "cpu.max").read_text() == "150000 100000"
    (path / "memory.events").write_text("low 0\noom 1\noom_kill 1\n")
    assert RunCgroups.oom_killed(path)
    for f in path.iterdir():  # a real cgroupfs removes these itself
        f.unlink()
    cg.destroy(path)
    assert not path.exists()


def test_builder_guard_reads_other_agents(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    builder, beta = root / "_builder", root / "beta"
    for d in (builder / "workspace", beta / "state", root / ".trash" / "old"):
        d.mkdir(parents=True)
    assert check_tool("Read", {"file_path": str(beta / "CLAUDE.md")}, builder, root) is None
    assert check_tool("Read", {"file_path": str(beta / "CLAUDE.md")}, builder) is not None
    assert check_tool("Read", {"file_path": str(beta / "state" / "x")}, builder, root)
    assert check_tool("Glob", {"path": str(root / ".trash" / "old")}, builder, root)
    assert check_tool("Write", {"file_path": str(beta / "x.md")}, builder, root)
    assert check_tool("Read", {"file_path": str(tmp_path / "other")}, builder, root)
