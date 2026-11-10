"""Agent lifecycle: create from template, list, get, validate, delete."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from playground_core.errors import AlreadyExistsError, NotFoundError, PermissionDeniedError
from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import Actor, AgentRecord, Diagnostic, Severity, Template
from playground_core.paths import validate_slug
from playground_core.ports import (
    AgentHistory,
    AgentRepository,
    AgentStore,
    Clock,
    TemplateCatalog,
)
from playground_core.validation import has_errors, validate_folder, wants_content

log = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "blank"


@dataclass(frozen=True, slots=True)
class AgentDetails:
    record: AgentRecord
    manifest_json: str
    diagnostics: list[Diagnostic]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    diagnostics: list[Diagnostic]

    @property
    def runnable(self) -> bool:
        return not has_errors(self.diagnostics)


def render_template(template: Template, *, name: str, description: str) -> dict[str, bytes]:
    """Fill placeholders and stamp name/description into agent.json."""
    files: dict[str, bytes] = {}
    for path, content in template.files.items():
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            files[path] = content
            continue
        # JSON files get JSON-escaped values; everything else is plain text.
        if path.endswith(".json"):
            n, d = json.dumps(name)[1:-1], json.dumps(description)[1:-1]
        else:
            n, d = name, description
        files[path] = text.replace("{{agent_name}}", n).replace("{{agent_description}}", d).encode()
    raw = files.get(MANIFEST_FILE, b"{}")
    manifest: dict[str, object] = json.loads(raw) if raw.strip() else {}
    manifest.update(name=name, description=description)
    files[MANIFEST_FILE] = (json.dumps(manifest, indent=2) + "\n").encode()
    return files


class AgentService:
    def __init__(
        self,
        store: AgentStore,
        repo: AgentRepository,
        templates: TemplateCatalog,
        clock: Clock,
        history: AgentHistory | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._templates = templates
        self._clock = clock
        self._history = history

    async def list_templates(self) -> Sequence[Template]:
        return await self._templates.list()

    async def list(self) -> Sequence[AgentRecord]:
        return await self._repo.list()

    async def get(self, slug: str) -> AgentDetails:
        record = await self.require(slug)
        try:
            manifest = await self._store.read(slug, MANIFEST_FILE)
            manifest_json = manifest.data.decode("utf-8", errors="replace")
        except NotFoundError:
            manifest_json = ""
        report = await self.validate(slug)
        return AgentDetails(record, manifest_json, report.diagnostics)

    async def create(
        self,
        slug: str,
        name: str,
        description: str,
        template_name: str = "",
        actor: Actor | None = None,
    ) -> AgentRecord:
        validate_slug(slug, allow_system=False)
        template = await self._templates.get(template_name or DEFAULT_TEMPLATE)
        name = name.strip() or slug
        # Validate before touching disk so a bad name never leaves a folder.
        AgentManifest.model_validate({"name": name, "description": description})
        if await self._repo.get(slug) is not None or await self._store.exists(slug):
            raise AlreadyExistsError(f"agent {slug!r} already exists")

        files = render_template(template, name=name, description=description)
        await self._store.create_agent(slug, files)
        now = self._clock.now()
        record = AgentRecord(slug, name, description, template.name, now, now)
        try:
            await self._repo.add(record)
        except Exception:
            await self._store.trash_agent(slug)
            raise
        await self._record(
            slug, f"Create agent from template {template.name!r}", actor or Actor.platform()
        )
        return record

    async def seed_system_agent(self, slug: str, files: Mapping[str, bytes]) -> bool:
        """Create a system agent (for example ``_builder``) from seed files if
        its folder is missing. An existing folder is left alone, so local
        changes survive upgrades. Returns True when the folder was created."""
        validate_slug(slug)
        if not slug.startswith("_"):
            raise ValueError(f"system agent slugs start with '_': {slug!r}")
        if await self._store.exists(slug):
            return False
        await self._store.create_agent(slug, files)
        await self.reconcile()
        await self._record(slug, "Seed system agent", Actor.platform())
        return True

    async def snapshot_all(self) -> None:
        """Start the history of agents that have none, and record edits made
        to folders outside the platform (for example on the host)."""
        if self._history is None:
            return
        for record in await self._repo.list():
            try:
                has_history = bool(await self._history.log(record.slug, limit=1))
            except Exception:
                log.exception("could not read history of %s", record.slug)
                continue
            message = (
                "Record changes made outside the platform" if has_history else ("Start history")
            )
            await self._record(record.slug, message, Actor.platform())

    async def _record(self, slug: str, message: str, actor: Actor) -> None:
        if self._history is None:
            return
        try:
            await self._history.commit(slug, message, actor)
        except Exception:
            log.exception("could not record history for agent %s", slug)

    async def delete(self, slug: str) -> None:
        await self.require(slug)
        if slug.startswith("_"):
            raise PermissionDeniedError(f"system agent {slug!r} can't be deleted")
        await self._repo.remove(slug)
        if await self._store.exists(slug):
            await self._store.trash_agent(slug)

    async def validate(self, slug: str) -> ValidationReport:
        await self.require(slug)
        entries = await self._store.tree(slug, include_hidden=False)
        files: dict[str, bytes] = {}
        for entry in entries:
            if entry.kind.value == "file" and wants_content(entry.path):
                try:
                    files[entry.path] = (await self._store.read(slug, entry.path)).data
                except NotFoundError:
                    continue  # deleted while validating
        paths = {e.path for e in entries}
        diags: list[Diagnostic] = []
        if MANIFEST_FILE not in files:
            diags.append(Diagnostic(MANIFEST_FILE, Severity.ERROR, "agent.json is missing"))
        diags += validate_folder(files, paths)
        return ValidationReport(diags)

    async def refresh_manifest(self, slug: str) -> None:
        """Re-read agent.json into the index (after a rollback)."""
        try:
            raw = (await self._store.read(slug, MANIFEST_FILE)).data
        except NotFoundError:
            raw = None
        await self.touch(slug, manifest=raw)

    async def touch(self, slug: str, *, manifest: bytes | None = None) -> None:
        """Record an edit; refresh name/description when agent.json changed."""
        record = await self._repo.get(slug)
        if record is None:
            return
        record = replace(record, updated_at=self._clock.now())
        if manifest is not None:
            try:
                m = AgentManifest.model_validate_json(manifest)
                record = replace(record, name=m.name, description=m.description)
            except ValueError:
                pass  # invalid manifest keeps the last good name
        await self._repo.update(record)

    async def reconcile(self) -> None:
        """Index folders that exist on disk but not in the repository (seeded
        system agents, manual copies) and drop rows whose folder is gone."""
        on_disk = set(await self._store.list_agent_slugs())
        indexed = {r.slug for r in await self._repo.list()}
        for slug in sorted(indexed - on_disk):
            log.warning("agent %s has no folder; removing from index", slug)
            await self._repo.remove(slug)
        for slug in sorted(on_disk - indexed):
            name, description = slug, ""
            try:
                raw = (await self._store.read(slug, MANIFEST_FILE)).data
                m = AgentManifest.model_validate_json(raw)
                name, description = m.name, m.description
            except (NotFoundError, ValueError):
                pass
            now = self._clock.now()
            await self._repo.add(AgentRecord(slug, name, description, "", now, now))
            log.info("indexed existing agent folder %s", slug)

    async def require(self, slug: str) -> AgentRecord:
        validate_slug(slug)
        record = await self._repo.get(slug)
        if record is None:
            raise NotFoundError(f"agent {slug!r} not found")
        return record
