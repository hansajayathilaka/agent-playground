"""TemplateCatalog backed by folders: ``<root>/<name>/template.json`` plus the
files the new agent folder starts with."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from playground_core.errors import NotFoundError
from playground_core.models import Template

_META = "template.json"


class FsTemplateCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _load(self, folder: Path) -> Template:
        meta_path = folder / _META
        meta: dict[str, str] = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        files: dict[str, bytes] = {}
        for p in sorted(folder.rglob("*")):
            if p.is_symlink() or not p.is_file() or p == meta_path:
                continue
            files[p.relative_to(folder).as_posix()] = p.read_bytes()
        return Template(folder.name, meta.get("description", ""), files)

    async def list(self) -> Sequence[Template]:
        def run() -> list[Template]:
            if not self._root.is_dir():
                return []
            return [
                self._load(d)
                for d in sorted(self._root.iterdir())
                if d.is_dir() and not d.is_symlink() and (d / _META).exists()
            ]

        return await asyncio.to_thread(run)

    async def get(self, name: str) -> Template:
        for t in await self.list():
            if t.name == name:
                return t
        raise NotFoundError(f"template {name!r} not found")
