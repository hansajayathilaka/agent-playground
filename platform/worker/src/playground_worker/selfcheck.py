"""``playground-worker-selfcheck``: can this worker confine runs?

Checks that bwrap can build a jail and, with ``--require-cgroups``, that the
delegated cgroup v2 subtree applies per-run limits to a jailed process.
CI runs it in the worker image; operators can run it with
``docker compose run --rm worker playground-worker-selfcheck``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from playground_worker.cgroups import CgroupLimits, RunCgroups
from playground_worker.config import WorkerSettings
from playground_worker.jail import SandboxUnavailableError, check_sandbox, find_bwrap
from playground_worker.launcher import join_cgroup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-cgroups", action="store_true")
    args = parser.parse_args(argv)
    sandbox = WorkerSettings().sandbox()
    try:
        check_sandbox(sandbox)
    except SandboxUnavailableError as exc:
        print(f"jail: FAILED: {exc}")
        return 1
    print(f"jail: ok (mode={sandbox.mode}, network={sandbox.network})")

    cgroups = RunCgroups(sandbox.cgroup_root)
    if not cgroups.available:
        print(f"cgroup limits: off ({cgroups.reason})")
        return 1 if args.require_cgroups else 0
    path = cgroups.create("selfcheck", CgroupLimits(memory_mb=256, cpus=0.5, max_processes=64))
    if path is None:
        print("cgroup limits: FAILED to create a run cgroup")
        return 1
    try:
        bwrap = find_bwrap() or "bwrap"
        cmd = [bwrap, "--ro-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "--dev", "/dev"]
        cmd += ["--die-with-parent", "--", "cat", "/proc/self/cgroup"]
        out = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            preexec_fn=lambda: join_cgroup(str(path)),
        ).stdout
        limit = Path(path, "memory.max").read_text().strip()
        print(f"cgroup limits: ok ({path}, memory.max={limit}, jailed process in {out.strip()})")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"cgroup limits: FAILED: {exc}")
        return 1
    finally:
        cgroups.destroy(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
