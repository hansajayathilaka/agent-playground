"""Agent folder history: record changes, list, diff, roll back.

Every change to an agent folder becomes a commit in its history, tagged with
who made it (a person, the Builder with its run id, or the agent's own run).
Recording never fails the change it records: the file is already written, so
a history error is logged and the next commit picks the change up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Sequence
from datetime import timedelta

from playground_core.errors import FailedPreconditionError, InvalidArgumentError, NotFoundError
from playground_core.models import Actor, Commit, FileChange
from playground_core.paths import is_platform_path, normalize_rel_path
from playground_core.ports import AgentHistory, JobQueue
from playground_core.services.agents import AgentService

log = logging.getLogger(__name__)

COMMIT_ID_RE = re.compile(r"^[0-9a-f]{4,64}$")
EDIT_LOCK_TTL = timedelta(seconds=60)
MAX_LOG = 500


async def record_change(
    history: AgentHistory | None, slug: str, message: str, actor: Actor
) -> Commit | None:
    if history is None:
        return None
    try:
        return await history.commit(slug, message, actor)
    except Exception:
        log.exception("could not record history for agent %s", slug)
        return None


def check_commit_id(value: str, *, allow_empty: bool = True) -> str:
    if (not value and allow_empty) or COMMIT_ID_RE.fullmatch(value):
        return value
    raise InvalidArgumentError(f"invalid commit id {value!r}")


@contextlib.asynccontextmanager
async def edit_lock(queue: JobQueue, slug: str, *, wait: float) -> AsyncGenerator[None]:
    """Hold the agent lock for an edit, so it never overlaps a run of that
    agent. Waits up to ``wait`` seconds for a running turn to finish."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        token = await queue.lock(slug, EDIT_LOCK_TTL)
        if token is not None:
            break
        if loop.time() >= deadline:
            raise FailedPreconditionError(
                f"agent {slug!r} is running; try again when its current turn finishes"
            )
        await asyncio.sleep(0.25)
    try:
        yield
    finally:
        await asyncio.shield(queue.unlock(slug, token))


class HistoryService:
    def __init__(
        self,
        history: AgentHistory,
        agents: AgentService,
        queue: JobQueue,
        *,
        lock_wait: float = 10.0,
    ) -> None:
        self._history = history
        self._agents = agents
        self._queue = queue
        self._lock_wait = lock_wait

    async def log(self, slug: str, *, limit: int = 100, path: str = "") -> Sequence[Commit]:
        await self._agents.require(slug)
        if path:
            path = normalize_rel_path(path).as_posix()
        return await self._history.log(slug, limit=max(1, min(limit or 100, MAX_LOG)), path=path)

    async def diff(
        self, slug: str, from_commit: str = "", to_commit: str = ""
    ) -> Sequence[FileChange]:
        await self._agents.require(slug)
        check_commit_id(from_commit)
        check_commit_id(to_commit)
        return await self._history.diff(slug, from_commit, to_commit)

    async def rollback(self, slug: str, commit_id: str, actor: Actor, path: str = "") -> Commit:
        """Restore the folder (or one file or folder) to ``commit_id`` and
        record that as a new commit; history is never rewritten."""
        await self._agents.require(slug)
        check_commit_id(commit_id, allow_empty=False)
        rel = ""
        if path:
            clean = normalize_rel_path(path)
            if is_platform_path(clean):
                raise InvalidArgumentError(f"{clean.parts[0]}/ is not part of the history")
            rel = clean.as_posix()
        async with edit_lock(self._queue, slug, wait=self._lock_wait):
            # Record anything not yet committed, so the rollback is undoable.
            await record_change(self._history, slug, "Record changes before rollback", actor)
            await self._history.restore(slug, commit_id, rel)
            what = f"{rel} " if rel else ""
            commit = await self._history.commit(slug, f"Roll back {what}to {commit_id[:12]}", actor)
        await self._agents.refresh_manifest(slug)
        if commit is None:
            heads = await self._history.log(slug, limit=1)
            if not heads:
                raise NotFoundError(f"agent {slug!r} has no history")
            return heads[0]
        return commit
