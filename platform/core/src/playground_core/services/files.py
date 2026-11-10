"""File operations with platform policy on top of the AgentStore port."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from playground_core.errors import InvalidArgumentError, PermissionDeniedError
from playground_core.manifest import MANIFEST_FILE
from playground_core.models import Actor, Diagnostic, FileContent, FileEntry, WriteResult
from playground_core.paths import UNDELETABLE_FILES, is_platform_path, normalize_rel_path
from playground_core.ports import AgentHistory, AgentStore
from playground_core.services.agents import AgentService
from playground_core.services.history import record_change
from playground_core.validation import validate_file

# Matches the gRPC default 4 MiB message limit with room for the envelope.
MAX_WRITE_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    result: WriteResult
    diagnostics: list[Diagnostic]


class FileService:
    def __init__(
        self, store: AgentStore, agents: AgentService, history: AgentHistory | None = None
    ) -> None:
        self._store = store
        self._agents = agents
        self._history = history

    async def _record(self, slug: str, message: str, actor: Actor | None) -> None:
        await record_change(self._history, slug, message, actor or Actor.platform())

    async def tree(self, slug: str, *, include_hidden: bool = False) -> Sequence[FileEntry]:
        await self._agents.require(slug)
        return await self._store.tree(slug, include_hidden=include_hidden)

    async def read(self, slug: str, path: str) -> FileContent:
        await self._agents.require(slug)
        return await self._store.read(slug, path)

    async def write(
        self,
        slug: str,
        path: str,
        data: bytes,
        *,
        if_match: str = "",
        create_only: bool = False,
        actor: Actor | None = None,
    ) -> WriteOutcome:
        await self._agents.require(slug)
        rel = self._writable(path)
        if len(data) > MAX_WRITE_BYTES:
            raise InvalidArgumentError(f"file too large ({len(data)} bytes, max {MAX_WRITE_BYTES})")
        result = await self._store.write(
            slug, path, data, if_match=if_match, create_only=create_only
        )
        key = rel.as_posix()
        await self._agents.touch(slug, manifest=data if key == MANIFEST_FILE else None)
        await self._record(slug, f"Update {key}", actor)
        return WriteOutcome(result, validate_file(key, data))

    async def delete(
        self, slug: str, path: str, *, recursive: bool = False, actor: Actor | None = None
    ) -> None:
        await self._agents.require(slug)
        rel = self._writable(path)
        if rel.as_posix() in UNDELETABLE_FILES:
            raise PermissionDeniedError(f"{rel.as_posix()} can't be deleted")
        await self._store.delete(slug, path, recursive=recursive)
        await self._agents.touch(slug)
        await self._record(slug, f"Delete {rel.as_posix()}", actor)

    async def move(
        self, slug: str, from_path: str, to_path: str, *, actor: Actor | None = None
    ) -> FileEntry:
        await self._agents.require(slug)
        src = self._writable(from_path)
        dst = self._writable(to_path)
        if src.as_posix() in UNDELETABLE_FILES:
            raise PermissionDeniedError(f"{src.as_posix()} can't be moved")
        entry = await self._store.move(slug, from_path, to_path)
        await self._agents.touch(slug)
        await self._record(slug, f"Move {src.as_posix()} to {dst.as_posix()}", actor)
        return entry

    async def mkdir(self, slug: str, path: str) -> FileEntry:
        await self._agents.require(slug)
        self._writable(path)
        return await self._store.mkdir(slug, path)

    @staticmethod
    def _writable(path: str) -> PurePosixPath:
        rel = normalize_rel_path(path)
        if is_platform_path(rel):
            raise PermissionDeniedError(f"{rel.parts[0]}/ is managed by the platform")
        return rel
