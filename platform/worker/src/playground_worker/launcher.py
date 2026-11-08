"""``playground-claude-launcher``: the ``cli_path`` the worker gives the SDK.

The Claude Agent SDK starts the CLI with the worker's whole environment plus
``options.env`` on top. The worker's environment holds platform secrets
(database URL, master key), so the runner passes the names of every variable
the worker itself had in ``PLAYGROUND_DROP_ENV``, and this launcher removes
them before exec'ing the real CLI. What is left is exactly the env the
platform built for the run plus the variables the SDK adds for its own
protocol.

This is also the seam for the Phase 3 outer sandbox: the launcher is the one
place every agent process starts from.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

DROP_VAR = "PLAYGROUND_DROP_ENV"
REAL_CLI_VAR = "PLAYGROUND_REAL_CLI"


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
    drop |= {DROP_VAR, REAL_CLI_VAR}
    return {k: v for k, v in environ.items() if k not in drop}


def main() -> None:
    real = os.environ.get(REAL_CLI_VAR) or bundled_cli()
    env = scrubbed_env(dict(os.environ))
    os.execve(real, [real, *sys.argv[1:]], env)  # noqa: S606 - fixed binary, no shell


if __name__ == "__main__":
    main()
