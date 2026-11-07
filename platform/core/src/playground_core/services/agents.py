"""Agent lifecycle: create from template, list, get, validate, delete."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from playground_core.errors import AlreadyExistsError, NotFoundError
from playground_core.manifest import MANIFEST_FILE, AgentManifest
from playground_core.models import AgentRecord, Diagnostic, Severity, Template
from playground_core.paths import validate_slug
from playground_core.ports import AgentRepository, AgentStore, Clock, TemplateCatalog
from playground_core.validation import VALIDATED_FILES, has_errors, validate_file

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
    ) -> None:
        self._store = store
        self._repo = repo
        self._templates = templates
        self._clock = clock

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
        self, slug: str, name: str, description: str, template_name: str = ""
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
        return record

    async def delete(self, slug: str) -> None:
        await self.require(slug)
        await self._repo.remove(slug)
        if await self._store.exists(slug):
            await self._store.trash_agent(slug)

    async def validate(self, slug: str) -> ValidationReport:
        await self.require(slug)
        diags: list[Diagnostic] = []
        for path in sorted(VALIDATED_FILES):
            try:
                content = await self._store.read(slug, path)
            except NotFoundError:
                if path == MANIFEST_FILE:
                    diags.append(Diagnostic(path, Severity.ERROR, "agent.json is missing"))
                continue
            diags += validate_file(path, content.data)
        return ValidationReport(diags)

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
