"""AgentHistory on git: one repository per agent in ``<agent>/.git``.

The git CLI runs with a fixed, minimal environment and configuration:
no global or system config, hooks off, symlinks checked out as plain files,
no external diff or textconv drivers, so content an agent wrote can't make
git run anything. ``state/`` is excluded through ``.git/info/exclude``.

The API and the worker may commit to the same repository at once (an edit
and a run's snapshot); git's own index lock serialises them and a busy lock
is retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from playground_core.errors import InvalidArgumentError, NotFoundError
from playground_core.models import Actor, ActorKind, ChangeKind, Commit, FileChange
from playground_core.paths import resolve_agent_path

log = logging.getLogger(__name__)

BRANCH = "main"
EXCLUDES = "# Managed by the platform: never part of the history.\n/state/\n"
MAX_PATCH_BYTES = 200_000
MAX_DIFF_FILES = 300
LOCK_RETRIES = 40
_SEP, _END = "\x1f", "\x1e"
_FIELDS = ["%H", "%an", "%aI", "%s", "%(trailers:key=Platform-Actor,valueonly,separator=)"]
_FIELDS.append("%(trailers:key=Platform-Run,valueonly,separator=)")
_LOG_FORMAT = _SEP.join(_FIELDS) + _END
_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.symlinks=false",
    "core.autocrlf=false",
    "core.fsmonitor=false",
    "core.quotePath=false",
    "commit.gpgSign=false",
    "gc.autoDetach=false",
    "diff.external=",
    "safe.directory=*",
)
_STATUS = {
    "A": ChangeKind.ADDED,
    "M": ChangeKind.MODIFIED,
    "T": ChangeKind.MODIFIED,
    "D": ChangeKind.DELETED,
    "R": ChangeKind.RENAMED,
    "C": ChangeKind.ADDED,
}


class GitError(RuntimeError):
    pass


class GitAgentHistory:
    def __init__(self, agents_root: Path, git: str = "git") -> None:
        self._root = agents_root
        self._git_bin = git
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, slug: str) -> asyncio.Lock:
        return self._locks.setdefault(slug, asyncio.Lock())

    def _dir(self, slug: str) -> Path:
        path, _ = resolve_agent_path(self._root, slug, allow_root=True)
        git_dir = path / ".git"
        if git_dir.is_symlink() or (git_dir.exists() and not git_dir.is_dir()):
            raise InvalidArgumentError(f"agent {slug!r} has an invalid .git entry")
        return path

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    async def _git(
        self, agent_dir: Path, *args: str, ok_codes: tuple[int, ...] = (0,)
    ) -> tuple[int, bytes]:
        argv = [
            self._git_bin,
            f"--git-dir={agent_dir / '.git'}",
            f"--work-tree={agent_dir}",
        ]
        for item in _CONFIG:
            argv += ["-c", item]
        argv += args
        for attempt in range(LOCK_RETRIES):
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=agent_dir,
                env=self._env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            code = proc.returncode or 0
            if code in ok_codes:
                return code, out
            text = err.decode(errors="replace")
            if ".lock" in text and "exists" in text and attempt < LOCK_RETRIES - 1:
                await asyncio.sleep(0.05 * (attempt + 1))  # another process holds the index
                continue
            raise GitError(f"git {args[0]} failed ({code}): {text.strip()[:500]}")
        raise GitError("unreachable")  # pragma: no cover

    async def _ensure_repo(self, agent_dir: Path) -> None:
        git_dir = agent_dir / ".git"
        if (git_dir / "HEAD").is_file():
            return
        await self._git(agent_dir, "init", "-q", f"--initial-branch={BRANCH}")
        info = git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text(EXCLUDES)

    async def _has_head(self, agent_dir: Path) -> bool:
        if not (agent_dir / ".git" / "HEAD").is_file():
            return False
        code, _ = await self._git(
            agent_dir, "rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1, 128)
        )
        return code == 0

    async def _resolve(self, agent_dir: Path, ref: str) -> str:
        code, out = await self._git(
            agent_dir, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", ok_codes=(0, 1, 128)
        )
        if code != 0:
            raise NotFoundError(f"commit {ref!r} not found")
        return out.decode().strip()

    # -- AgentHistory ------------------------------------------------------------

    async def commit(self, slug: str, message: str, actor: Actor) -> Commit | None:
        agent_dir = self._dir(slug)
        async with self._lock(slug):
            await self._ensure_repo(agent_dir)
            await self._git(agent_dir, "add", "-A", "--", ".")
            has_head = await self._has_head(agent_dir)
            if has_head:
                code, _ = await self._git(
                    agent_dir, "diff", "--cached", "--quiet", "--no-ext-diff", ok_codes=(0, 1)
                )
                if code == 0:
                    return None
            else:
                _, staged = await self._git(agent_dir, "ls-files", "-z")
                if not staged:
                    return None
            subject = " ".join(message.split())[:200] or "Update"
            trailers = [f"Platform-Actor: {actor.kind.value}"]
            if actor.run_id:
                trailers.append(f"Platform-Run: {actor.run_id}")
            body = subject + "\n\n" + "\n".join(trailers) + "\n"
            name = " ".join(actor.name.split())[:100] or actor.kind.value
            await self._commit(agent_dir, name, actor, body)
            return (await self._log(agent_dir, 1, ""))[0]

    async def _commit(self, agent_dir: Path, name: str, actor: Actor, body: str) -> None:
        msg_file = agent_dir / ".git" / "PLATFORM_COMMIT_MSG"
        msg_file.write_text(body)
        try:
            await self._git(
                agent_dir,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={actor.kind.value}@agent-playground.invalid",
                "commit",
                "-q",
                "--no-verify",
                "--cleanup=whitespace",
                "-F",
                str(msg_file),
            )
        finally:
            with contextlib.suppress(OSError):
                msg_file.unlink()

    async def log(self, slug: str, *, limit: int, path: str = "") -> Sequence[Commit]:
        agent_dir = self._dir(slug)
        if not await self._has_head(agent_dir):
            return []
        return await self._log(agent_dir, limit, path)

    async def _log(self, agent_dir: Path, limit: int, path: str) -> list[Commit]:
        args = ["log", f"-n{max(1, limit)}", f"--format={_LOG_FORMAT}", "HEAD"]
        if path:
            args += ["--", f":(literal){path}"]
        _, out = await self._git(agent_dir, *args)
        commits: list[Commit] = []
        for raw in out.decode("utf-8", errors="replace").split(_END):
            raw = raw.strip("\n")
            if not raw:
                continue
            cid, author, when, subject, actor, run_id = (raw.split(_SEP) + [""] * 6)[:6]
            try:
                kind = ActorKind(actor.strip())
            except ValueError:
                kind = ActorKind.PLATFORM
            commits.append(
                Commit(
                    id=cid,
                    message=subject,
                    author=author,
                    actor=kind,
                    time=datetime.fromisoformat(when),
                    run_id=run_id.strip(),
                )
            )
        return commits

    async def diff(self, slug: str, from_commit: str, to_commit: str) -> Sequence[FileChange]:
        agent_dir = self._dir(slug)
        if not await self._has_head(agent_dir):
            if from_commit or to_commit:
                raise NotFoundError(f"agent {slug!r} has no history yet")
            return []
        to_id = await self._resolve(agent_dir, to_commit or "HEAD")
        if from_commit:
            from_id = await self._resolve(agent_dir, from_commit)
        else:
            code, out = await self._git(
                agent_dir, "rev-parse", "--verify", "-q", f"{to_id}^", ok_codes=(0, 1, 128)
            )
            if code == 0:
                from_id = out.decode().strip()
            else:  # first commit: compare with the empty tree
                _, empty = await self._git(agent_dir, "hash-object", "-t", "tree", os.devnull)
                from_id = empty.decode().strip()
        common = ["--no-color", "--no-ext-diff", "--no-textconv", "-M", from_id, to_id]
        _, status_out = await self._git(agent_dir, "diff", "--name-status", "-z", *common)
        _, num_out = await self._git(agent_dir, "diff", "--numstat", "-z", *common)
        changes = _parse_name_status(status_out.decode("utf-8", errors="replace"))
        counts = _parse_numstat(num_out.decode("utf-8", errors="replace"))
        result: list[FileChange] = []
        for i, (kind, path, old) in enumerate(changes):
            added, deleted, binary = counts.get(path, (0, 0, False))
            patch, truncated = "", False
            if not binary and i < MAX_DIFF_FILES:
                specs = [f":(literal){p}" for p in ((old, path) if old else (path,))]
                _, raw = await self._git(agent_dir, "diff", *common, "--", *specs)
                truncated = len(raw) > MAX_PATCH_BYTES
                patch = raw[:MAX_PATCH_BYTES].decode("utf-8", errors="replace")
            elif i >= MAX_DIFF_FILES:
                truncated = True
            result.append(FileChange(path, kind, old, binary, added, deleted, patch, truncated))
        return result

    async def restore(self, slug: str, commit_id: str, path: str = "") -> None:
        agent_dir = self._dir(slug)
        if not await self._has_head(agent_dir):
            raise NotFoundError(f"agent {slug!r} has no history yet")
        cid = await self._resolve(agent_dir, commit_id)
        async with self._lock(slug):
            if path:
                code, _ = await self._git(
                    agent_dir,
                    "cat-file",
                    "-e",
                    f"{cid}:{path}",
                    ok_codes=(0, 1, 128),
                )
                in_target = code == 0
                code, _ = await self._git(
                    agent_dir, "cat-file", "-e", f"HEAD:{path}", ok_codes=(0, 1, 128)
                )
                if not in_target and code != 0:
                    raise NotFoundError(f"{path} is in neither version")
                spec = f":(literal){path}"
            else:
                spec = ":/"
            # Files added since the commit are removed; changed ones rewritten.
            await self._git(
                agent_dir, "restore", f"--source={cid}", "--staged", "--worktree", "--", spec
            )


def _parse_name_status(text: str) -> list[tuple[ChangeKind, str, str]]:
    parts = text.split("\0")
    out: list[tuple[ChangeKind, str, str]] = []
    i = 0
    while i < len(parts) and parts[i]:
        code = parts[i][0]
        kind = _STATUS.get(code, ChangeKind.MODIFIED)
        if code in "RC":
            old, new = parts[i + 1], parts[i + 2]
            out.append((kind, new, old if code == "R" else ""))
            i += 3
        else:
            out.append((kind, parts[i + 1], ""))
            i += 2
    return out


def _parse_numstat(text: str) -> dict[str, tuple[int, int, bool]]:
    """``added\\tdeleted\\tpath\\0`` or, for renames, ``added\\tdeleted\\t\\0old\\0new\\0``."""
    parts = text.split("\0")
    out: dict[str, tuple[int, int, bool]] = {}
    i = 0
    while i < len(parts) and parts[i]:
        added, deleted, path = parts[i].split("\t", 2)
        if path == "":
            path = parts[i + 2]
            i += 3
        else:
            i += 1
        binary = added == "-"
        out[path] = (0 if binary else int(added), 0 if binary else int(deleted), binary)
    return out
