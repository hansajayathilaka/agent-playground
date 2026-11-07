"""AgentStore on a local filesystem volume."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from playground_core.errors import (
    AlreadyExistsError,
    ConflictError,
    InvalidArgumentError,
    NotFoundError,
)
from playground_core.models import FileContent, FileEntry, FileKind, WriteResult
from playground_core.paths import (
    PLATFORM_DIRS,
    normalize_rel_path,
    resolve_agent_path,
    validate_slug,
)


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(path: Path, rel: PurePosixPath) -> FileEntry:
    st = path.lstat()
    is_dir = path.is_dir()
    return FileEntry(
        path=rel.as_posix(),
        kind=FileKind.DIRECTORY if is_dir else FileKind.FILE,
        size=0 if is_dir else st.st_size,
        modified_at=datetime.fromtimestamp(st.st_mtime, UTC),
    )


class LocalAgentStore:
    """Each agent is ``<agents_root>/<slug>``. Deleted agents move to
    ``<agents_root>/.trash`` (same volume, so the move is a rename)."""

    def __init__(self, agents_root: Path) -> None:
        self._root = agents_root
        self._trash = agents_root / ".trash"
        self._locks: dict[str, asyncio.Lock] = {}
        self._root.mkdir(parents=True, exist_ok=True)

    def _lock(self, slug: str) -> asyncio.Lock:
        return self._locks.setdefault(slug, asyncio.Lock())

    def _resolve(
        self, slug: str, rel: str, *, allow_root: bool = False
    ) -> tuple[Path, PurePosixPath]:
        return resolve_agent_path(self._root, slug, rel, allow_root=allow_root)

    # -- agents ---------------------------------------------------------------

    async def create_agent(self, slug: str, files: Mapping[str, bytes]) -> None:
        validate_slug(slug)
        clean = {normalize_rel_path(p): data for p, data in files.items()}
        await asyncio.to_thread(self._create_agent_sync, slug, clean)

    def _create_agent_sync(self, slug: str, files: Mapping[PurePosixPath, bytes]) -> None:
        final = self._root / slug
        if final.exists() or final.is_symlink():
            raise AlreadyExistsError(f"agent {slug!r} already exists")
        staging = self._root / f".tmp-{slug}-{secrets.token_hex(4)}"
        staging.mkdir()
        try:
            for rel, data in files.items():
                target = staging.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            try:
                staging.rename(final)
            except OSError as e:
                raise AlreadyExistsError(f"agent {slug!r} already exists") from e
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    async def trash_agent(self, slug: str) -> None:
        root, _ = self._resolve(slug, "", allow_root=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        dest = self._trash / f"{slug}-{stamp}-{secrets.token_hex(3)}"

        def move() -> None:
            self._trash.mkdir(exist_ok=True)
            root.rename(dest)

        async with self._lock(slug):
            await asyncio.to_thread(move)

    async def list_agent_slugs(self) -> Sequence[str]:
        def scan() -> list[str]:
            out: list[str] = []
            for e in os.scandir(self._root):
                if not e.is_dir(follow_symlinks=False):
                    continue
                try:
                    out.append(validate_slug(e.name))
                except InvalidArgumentError:
                    continue  # .trash, staging dirs, stray folders
            return sorted(out)

        return await asyncio.to_thread(scan)

    async def exists(self, slug: str) -> bool:
        validate_slug(slug)
        p = self._root / slug
        return p.is_dir() and not p.is_symlink()

    # -- files ----------------------------------------------------------------

    async def tree(self, slug: str, *, include_hidden: bool) -> Sequence[FileEntry]:
        root, _ = self._resolve(slug, "", allow_root=True)

        def walk(directory: Path, rel: PurePosixPath, out: list[FileEntry]) -> None:
            with os.scandir(directory) as it:
                items = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name))
            for e in items:
                if e.is_symlink():
                    continue  # never surfaced, never followed
                child_rel = rel / e.name
                if not include_hidden and not rel.parts and e.name in PLATFORM_DIRS:
                    continue
                path = Path(e.path)
                out.append(_entry(path, child_rel))
                if e.is_dir(follow_symlinks=False):
                    walk(path, child_rel, out)

        def run() -> list[FileEntry]:
            out: list[FileEntry] = []
            walk(root, PurePosixPath(), out)
            return out

        return await asyncio.to_thread(run)

    async def read(self, slug: str, path: str) -> FileContent:
        target, rel = self._resolve(slug, path)

        def run() -> FileContent:
            if not target.is_file():
                if target.is_dir():
                    raise InvalidArgumentError(f"{rel.as_posix()} is a directory")
                raise NotFoundError(f"{rel.as_posix()} not found")
            data = target.read_bytes()
            return FileContent(_entry(target, rel), etag_of(data), data)

        return await asyncio.to_thread(run)

    async def write(
        self, slug: str, path: str, data: bytes, *, if_match: str = "", create_only: bool = False
    ) -> WriteResult:
        target, rel = self._resolve(slug, path)

        def run() -> WriteResult:
            exists = target.exists()
            if target.is_dir():
                raise InvalidArgumentError(f"{rel.as_posix()} is a directory")
            if create_only and exists:
                raise AlreadyExistsError(f"{rel.as_posix()} already exists")
            if if_match:
                current = etag_of(target.read_bytes()) if exists else ""
                if current != if_match:
                    raise ConflictError(f"{rel.as_posix()} changed since it was read")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
            return WriteResult(_entry(target, rel), etag_of(data))

        async with self._lock(slug):
            return await asyncio.to_thread(run)

    async def delete(self, slug: str, path: str, *, recursive: bool) -> None:
        target, rel = self._resolve(slug, path)

        def run() -> None:
            if target.is_dir():
                if recursive:
                    shutil.rmtree(target)
                elif any(target.iterdir()):
                    raise InvalidArgumentError(f"{rel.as_posix()} is not empty")
                else:
                    target.rmdir()
            elif target.exists():
                target.unlink()
            else:
                raise NotFoundError(f"{rel.as_posix()} not found")

        async with self._lock(slug):
            await asyncio.to_thread(run)

    async def move(self, slug: str, from_path: str, to_path: str) -> FileEntry:
        src, src_rel = self._resolve(slug, from_path)
        dst, dst_rel = self._resolve(slug, to_path)
        if dst_rel.is_relative_to(src_rel):
            raise InvalidArgumentError("can't move a folder into itself")

        def run() -> FileEntry:
            if not src.exists():
                raise NotFoundError(f"{src_rel.as_posix()} not found")
            if dst.exists():
                raise AlreadyExistsError(f"{dst_rel.as_posix()} already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return _entry(dst, dst_rel)

        async with self._lock(slug):
            return await asyncio.to_thread(run)

    async def mkdir(self, slug: str, path: str) -> FileEntry:
        target, rel = self._resolve(slug, path)

        def run() -> FileEntry:
            if target.exists():
                raise AlreadyExistsError(f"{rel.as_posix()} already exists")
            target.mkdir(parents=True)
            return _entry(target, rel)

        async with self._lock(slug):
            return await asyncio.to_thread(run)
