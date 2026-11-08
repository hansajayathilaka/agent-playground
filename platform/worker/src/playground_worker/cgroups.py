"""cgroup v2 limits for one run (memory, CPU, process count).

The worker gets a delegated cgroup subtree (``RUN_CGROUP_ROOT``, set up by
the container entrypoint): the worker itself sits in ``<root>/main`` and each
run gets ``<root>/run-<id>``. The launcher writes its own pid into the run's
``cgroup.procs`` before exec'ing bwrap, so the whole CLI tree is inside, and
``cgroup.kill`` ends every process of the run at once.

Without a usable subtree (no cgroup v2, no privileges) runs still work; the
wall-clock timeout and rlimits still apply, and the worker logs a warning.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CONTROLLERS = ("memory", "pids", "cpu")
CPU_PERIOD_US = 100_000


@dataclass(frozen=True, slots=True)
class CgroupLimits:
    memory_mb: int
    cpus: float
    max_processes: int


def _controllers(path: Path) -> set[str]:
    try:
        return set((path / "cgroup.subtree_control").read_text().split())
    except OSError:
        return set()


class RunCgroups:
    def __init__(self, root: Path | None) -> None:
        self._root = root
        self.available = False
        self.reason = "RUN_CGROUP_ROOT is not set"
        if root is None:
            return
        missing = set(CONTROLLERS) - _controllers(root)
        if not root.is_dir():
            self.reason = f"{root} does not exist"
        elif missing:
            self.reason = f"{root} does not delegate {', '.join(sorted(missing))}"
        elif not os.access(root, os.W_OK):
            self.reason = f"{root} is not writable"
        else:
            self.available = True
            self.reason = ""

    def create(self, run_id: str, limits: CgroupLimits) -> Path | None:
        if not self.available or self._root is None:
            return None
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")[:64]
        path = self._root / f"run-{safe}"
        try:
            path.mkdir(exist_ok=True)
            mem = limits.memory_mb * 1024 * 1024
            (path / "memory.max").write_text(str(mem))
            with contextlib.suppress(OSError):  # absent without swap accounting
                (path / "memory.swap.max").write_text("0")
            (path / "pids.max").write_text(str(limits.max_processes))
            quota = max(1000, int(limits.cpus * CPU_PERIOD_US))
            (path / "cpu.max").write_text(f"{quota} {CPU_PERIOD_US}")
        except OSError:
            log.warning("could not create run cgroup %s", path, exc_info=True)
            self.destroy(path)
            return None
        return path

    def destroy(self, path: Path | None) -> None:
        """Kill everything left in the run's cgroup and remove it."""
        if path is None:
            return
        kill = path / "cgroup.kill"
        with contextlib.suppress(OSError):
            if kill.exists():
                kill.write_text("1")
        for _ in range(50):
            try:
                path.rmdir()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)
        log.warning("run cgroup %s is still busy; leaving it", path)

    @staticmethod
    def oom_killed(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            for line in (path / "memory.events").read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill" and int(value or 0) > 0:
                    return True
        except (OSError, ValueError):
            pass
        return False
