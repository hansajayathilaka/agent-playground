"""``playground-claude-launcher``: the ``cli_path`` the worker gives the SDK.

The Claude Agent SDK starts the CLI with the worker's whole environment plus
``options.env``. The worker's environment holds platform secrets (database
URL, master key), so the runner passes the names of every variable the
worker itself had in ``PLAYGROUND_DROP_ENV``, and this launcher removes them.

When the run carries a jail spec (``PLAYGROUND_JAIL``), the launcher also
joins the run's cgroup, applies rlimits and execs ``bwrap`` around the real
CLI, so every process the CLI starts (Bash, hooks, stdio MCP servers) is
confined. See ``jail.py``.
"""

from __future__ import annotations

import importlib.util
import os
import resource
import sys
from pathlib import Path

from playground_worker.jail import JAIL_VAR, JailSpec, bwrap_args, find_bwrap

DROP_VAR = "PLAYGROUND_DROP_ENV"
REAL_CLI_VAR = "PLAYGROUND_REAL_CLI"

RLIMITS = {
    "core": resource.RLIMIT_CORE,
    "nofile": resource.RLIMIT_NOFILE,
    "fsize": resource.RLIMIT_FSIZE,
}


def bundled_cli() -> str:
    """The CLI shipped inside the claude-agent-sdk wheel (found without
    importing the SDK, which is slow)."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        raise SystemExit("claude-agent-sdk is not installed")
    cli = Path(spec.origin).parent / "_bundled" / "claude"
    if not cli.is_file():
        raise SystemExit(f"bundled Claude CLI not found at {cli}")
    return str(cli)


def scrubbed_env(environ: dict[str, str]) -> dict[str, str]:
    drop = {k for k in environ.get(DROP_VAR, "").split(",") if k}
    drop |= {DROP_VAR, REAL_CLI_VAR, JAIL_VAR}
    return {k: v for k, v in environ.items() if k not in drop}


def apply_rlimits(limits: dict[str, int]) -> None:
    for name, value in limits.items():
        which = RLIMITS.get(name)
        if which is None:
            continue
        _, hard = resource.getrlimit(which)
        soft = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(which, (soft, soft))


def join_cgroup(path: str) -> None:
    with Path(path, "cgroup.procs").open("w") as f:
        f.write(str(os.getpid()))


def main() -> None:
    real = os.environ.get(REAL_CLI_VAR) or bundled_cli()
    raw_spec = os.environ.get(JAIL_VAR, "")
    env = scrubbed_env(dict(os.environ))
    command = [real, *sys.argv[1:]]
    if not raw_spec:
        os.execve(real, command, env)  # noqa: S606 - fixed binary, no shell
    spec = JailSpec.from_json(raw_spec)
    bwrap = find_bwrap()
    if bwrap is None:
        raise SystemExit("playground-claude-launcher: bwrap is not installed")
    if spec.cgroup:
        join_cgroup(spec.cgroup)
    apply_rlimits(spec.rlimits)
    os.execve(bwrap, [bwrap, *bwrap_args(spec, command)], env)  # noqa: S606


if __name__ == "__main__":
    main()
