"""The outer bubblewrap jail around a run's whole Claude CLI process tree.

The SDK's own sandbox only wraps Bash. Hooks, stdio MCP servers and the file
tools run as plain processes, so the platform jails the CLI itself: the
launcher (``cli_path``) execs ``bwrap`` with the argv built here, and every
process the CLI starts inherits the jail.

What a jailed run sees:

- System directories (``/usr``, ``/lib*``, a short list from ``/etc``) and the
  bundled CLI, read-only. No ``/home``, ``/data``, ``/opt`` or worker files.
- Its own agent folder at its real path, read-only, with ``memory/`` and
  ``workspace/`` bound writable and ``state/`` + ``.git/`` hidden.
- Its per-agent session dir (HOME, CLAUDE_CONFIG_DIR, TMPDIR), writable.
- Fresh ``/proc`` (own PID namespace), ``/dev`` and ``/tmp``.
- With ``network="proxy"``: its own network namespace with only loopback. A
  tiny forwarder (``jail_init``) inside the jail relays 127.0.0.1:PROXY_PORT
  to the worker's egress proxy over a bound unix socket, so HTTP(S) through
  the proxy is the only way out.

This module is imported by the launcher, so it stays stdlib-only and cheap.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

JAIL_VAR = "PLAYGROUND_JAIL"
PROXY_PORT = 3128
# Where the egress socket and jail_init appear inside the jail.
JAIL_RUN_DIR = "/run/playground"
JAIL_EGRESS_SOCKET = f"{JAIL_RUN_DIR}/egress.sock"
JAIL_INIT_PATH = f"{JAIL_RUN_DIR}/jail_init.py"

HIDDEN_DIRS = ("state", ".git")
WRITABLE_DIRS = ("memory", "workspace")

SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# Only what programs need to run, resolve users and verify TLS.
ETC_ENTRIES = (
    "alternatives",
    "ca-certificates",
    "ca-certificates.conf",
    "crypto-policies",
    "gai.conf",
    "group",
    "host.conf",
    "hosts",
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "localtime",
    "mime.types",
    "nsswitch.conf",
    "passwd",
    "pki",
    "protocols",
    "resolv.conf",
    "services",
    "ssl",
    "timezone",
)


class JailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JailSpec:
    """Everything the launcher needs to build the bwrap command. Paths are
    absolute, already resolved by the worker."""

    agent_dir: str
    session_dir: str
    cli_dir: str
    # "proxy": own netns, egress only through the worker's proxy.
    # "host": share the worker's network (no egress control).
    network: str = "proxy"
    egress_socket: str = ""
    # stdlib-only Python for jail_init (needed in proxy mode).
    python: str = ""
    python_prefixes: tuple[str, ...] = ()
    jail_init: str = ""
    # Extra read-only paths (CA bundles named by passthrough env vars).
    ro_files: tuple[str, ...] = ()
    # Builder: every agent folder readable, state/.git hidden in each.
    agents_root: str = ""
    readable_agents: tuple[str, ...] = ()
    # cgroup v2 directory the launcher joins before exec; empty = none.
    cgroup: str = ""
    # setrlimit values applied by the launcher.
    rlimits: dict[str, int] = field(default_factory=dict[str, int])

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> JailSpec:
        data = json.loads(raw)
        for key in ("python_prefixes", "ro_files", "readable_agents"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


def find_bwrap() -> str | None:
    return shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")


def _real_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _hide(args: list[str], path: str) -> None:
    if _real_dir(path):
        args += ["--tmpfs", path, "--remount-ro", path]


def _bind_system(args: list[str]) -> None:
    for d in SYSTEM_DIRS:
        if os.path.islink(d):
            args += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            args += ["--ro-bind", d, d]
    for name in ETC_ENTRIES:
        p = f"/etc/{name}"
        if os.path.lexists(p):
            args += ["--ro-bind-try", p, p]


def _bind_agent(args: list[str], agent_dir: str) -> None:
    args += ["--ro-bind", agent_dir, agent_dir]
    for name in HIDDEN_DIRS:
        _hide(args, os.path.join(agent_dir, name))
    for name in WRITABLE_DIRS:
        d = os.path.join(agent_dir, name)
        # A symlinked writable dir would bind its (outside) target writable.
        if not _real_dir(d):
            raise JailError(f"{name}/ must be a real directory inside the agent folder")
        args += ["--bind", d, d]


def bwrap_args(spec: JailSpec, command: list[str]) -> list[str]:
    """The bwrap argv (without the bwrap binary) that runs ``command``."""
    agent_dir = spec.agent_dir
    args: list[str] = [
        "--die-with-parent",
        "--new-session",
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
        "--hostname",
        "agent",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 - a fresh tmpfs inside the jail
        "--tmpfs",
        "/run",
    ]
    if spec.network == "proxy":
        args.append("--unshare-net")
    elif spec.network != "host":
        raise JailError(f"unknown jail network mode {spec.network!r}")
    _bind_system(args)
    args += ["--ro-bind", spec.cli_dir, spec.cli_dir]
    for f in spec.ro_files:
        args += ["--ro-bind-try", f, f]

    if spec.agents_root:
        # Builder view: every agent readable, nothing else of the data volume.
        args += ["--ro-bind", spec.agents_root, spec.agents_root]
        _hide(args, os.path.join(spec.agents_root, ".trash"))
        for other in spec.readable_agents:
            for name in HIDDEN_DIRS:
                _hide(args, os.path.join(other, name))
    _bind_agent(args, agent_dir)
    args += ["--bind", spec.session_dir, spec.session_dir]

    if spec.network == "proxy":
        if not (spec.egress_socket and spec.python and spec.jail_init):
            raise JailError("proxy network mode needs an egress socket, python and jail_init")
        for prefix in spec.python_prefixes:
            args += ["--ro-bind", prefix, prefix]
        args += [
            "--ro-bind",
            spec.jail_init,
            JAIL_INIT_PATH,
            "--bind",
            spec.egress_socket,
            JAIL_EGRESS_SOCKET,
        ]
        command = [
            spec.python,
            "-I",
            "-S",
            JAIL_INIT_PATH,
            JAIL_EGRESS_SOCKET,
            str(PROXY_PORT),
            "--",
            *command,
        ]
    args += ["--chdir", agent_dir, "--", *command]
    return args


def python_for_jail() -> tuple[str, tuple[str, ...]]:
    """The interpreter running this worker and the dirs it needs (stdlib)."""
    exe = os.path.realpath(sys.executable)
    prefixes = {os.path.realpath(sys.base_prefix)}
    under_system = ("/usr/", "/bin/", "/lib")
    paths = [p for p in sorted(prefixes) if not p.startswith(under_system) and p != "/usr"]
    exe_dir = os.path.dirname(exe)
    if not exe.startswith(under_system) and not any(exe.startswith(p + "/") for p in paths):
        paths.append(exe_dir)
    return exe, tuple(paths)


def jail_init_file() -> str:
    return str(Path(__file__).with_name("jail_init.py"))


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """How runs are confined (see jail.py). ``mode="none"`` runs the CLI
    unjailed: development only, never in the worker image."""

    mode: Literal["bwrap", "none"] = "bwrap"
    network: Literal["proxy", "host"] = "proxy"
    # Delegated cgroup v2 subtree for per-run limits; None = no cgroup limits.
    cgroup_root: Path | None = None
    # Worker-owned dir for the per-run egress sockets.
    runtime_dir: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "playground-runtime"
    )
    timeout_seconds: int = 3600
    memory_mb: int = 4096
    cpus: float = 2.0
    max_processes: int = 512
    max_file_mb: int = 4096
    max_open_files: int = 4096
    # This agent may read every agent folder (not their state/ or .git/).
    builder_slug: str = "_builder"


class SandboxUnavailableError(RuntimeError):
    pass


def check_sandbox(settings: SandboxSettings) -> None:
    """Fail fast at worker start if bwrap can't create a jail here."""
    if settings.mode == "none":
        return
    bwrap = find_bwrap()
    if bwrap is None:
        raise SandboxUnavailableError("bwrap is not installed (SANDBOX_MODE=bwrap)")
    probe = [bwrap, "--ro-bind", "/", "/", "--unshare-pid", "--unshare-net", "--proc", "/proc"]
    probe += ["--dev", "/dev", "--die-with-parent", "--", "/bin/true"]
    done = subprocess.run(probe, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    if done.returncode != 0:
        raise SandboxUnavailableError(
            "bwrap cannot create a sandbox in this environment "
            f"({done.stderr.strip() or done.returncode}). The worker needs user namespaces "
            "and permission to mount /proc; see deploy/compose.yaml."
        )
