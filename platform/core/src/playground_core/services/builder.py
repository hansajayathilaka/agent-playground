"""The Builder's platform tools, as plain async methods over core services.

The worker exposes these to Builder runs as an in-process MCP server (they
run in the worker, outside the run's jail). Every path goes through the same
services the API uses, so ``resolve_agent_path()``, the platform-path rules,
validation and history all apply. Guardrails:

- The Builder reads any agent but never writes its own folder.
- It never sees ``state/``, ``.git/`` or secret values; it may pick a
  credential set by name.
- Each change takes the target agent's lock (so it never lands in the middle
  of a run) and becomes a commit tagged with the Builder's run id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from playground_core.errors import InvalidArgumentError, PermissionDeniedError
from playground_core.events import EventKind
from playground_core.manifest import MANIFEST_FILE
from playground_core.models import Actor, Commit, Diagnostic, FileChange, FileKind, RunStatus
from playground_core.ports import EventLog, JobQueue
from playground_core.services.agents import AgentService
from playground_core.services.conversations import ConversationService
from playground_core.services.credentials import CredentialService
from playground_core.services.files import FileService
from playground_core.services.history import HistoryService, edit_lock

MAX_READ_CHARS = 60_000
MAX_REPLY_CHARS = 4_000
DEFAULT_TEST_TIMEOUT = 180.0
MAX_TEST_TIMEOUT = 900.0
TEST_TITLE_PREFIX = "Builder test: "

Result = dict[str, Any]


def _diag(d: Diagnostic) -> Result:
    out: Result = {"path": d.path, "severity": d.severity.value, "message": d.message}
    if d.location:
        out["location"] = d.location
    return out


def _commit(c: Commit) -> Result:
    out: Result = {
        "id": c.id[:12],
        "message": c.message,
        "author": c.author,
        "actor": c.actor.value,
        "time": c.time.isoformat(),
    }
    if c.run_id:
        out["run_id"] = c.run_id
    return out


def _change(c: FileChange) -> Result:
    out: Result = {"path": c.path, "change": c.change.value}
    if c.old_path:
        out["old_path"] = c.old_path
    if c.binary:
        out["binary"] = True
    else:
        out["patch"] = c.patch
    if c.truncated:
        out["truncated"] = True
    return out


class BuilderToolkit:
    def __init__(
        self,
        *,
        agents: AgentService,
        files: FileService,
        history: HistoryService,
        credentials: CredentialService,
        conversations: ConversationService,
        events: EventLog,
        queue: JobQueue,
        builder_slug: str,
        run_id: str,
        lock_wait: float = 30.0,
        poll_interval: float = 0.5,
    ) -> None:
        self._agents = agents
        self._files = files
        self._history = history
        self._credentials = credentials
        self._conversations = conversations
        self._events = events
        self._queue = queue
        self._builder_slug = builder_slug
        self._actor = Actor.builder(run_id)
        self._lock_wait = lock_wait
        self._poll = poll_interval

    def _writable_agent(self, slug: str) -> str:
        if slug == self._builder_slug:
            raise PermissionDeniedError(
                "the Builder can't change its own folder; ask the user to edit it"
            )
        return slug

    # -- reading -----------------------------------------------------------------

    async def list_agents(self) -> Result:
        records = await self._agents.list()
        return {
            "agents": [
                {"slug": r.slug, "name": r.name, "description": r.description}
                for r in records
                if r.slug != self._builder_slug
            ]
        }

    async def list_templates(self) -> Result:
        return {
            "templates": [
                {"name": t.name, "description": t.description, "files": sorted(t.files)}
                for t in await self._agents.list_templates()
            ]
        }

    async def list_credential_sets(self) -> Result:
        """Names only: the Builder never sees endpoints' secrets."""
        return {
            "credential_sets": [
                {
                    "name": c.name,
                    "description": c.description,
                    "provider": c.provider.value,
                    "default_model": c.default_model,
                    "is_default": c.is_default,
                }
                for c in await self._credentials.list()
            ]
        }

    async def get_agent(self, slug: str) -> Result:
        details = await self._agents.get(slug)
        entries = await self._files.tree(slug)
        recent = await self._history.log(slug, limit=5)
        return {
            "slug": slug,
            "name": details.record.name,
            "description": details.record.description,
            "agent_json": details.manifest_json,
            "files": [{"path": e.path, "size": e.size} for e in entries if e.kind is FileKind.FILE],
            "directories": [e.path for e in entries if e.kind is FileKind.DIRECTORY],
            "diagnostics": [_diag(d) for d in details.diagnostics],
            "recent_changes": [_commit(c) for c in recent],
        }

    async def read_agent_file(self, slug: str, path: str) -> Result:
        content = await self._files.read(slug, path)
        try:
            text = content.data.decode("utf-8")
        except UnicodeDecodeError:
            return {"path": content.entry.path, "binary": True, "size": content.entry.size}
        out: Result = {"path": content.entry.path, "content": text[:MAX_READ_CHARS]}
        if len(text) > MAX_READ_CHARS:
            out["truncated"] = True
        return out

    async def validate_agent(self, slug: str) -> Result:
        report = await self._agents.validate(slug)
        return {"runnable": report.runnable, "diagnostics": [_diag(d) for d in report.diagnostics]}

    async def agent_history(self, slug: str, limit: int = 20) -> Result:
        return {"commits": [_commit(c) for c in await self._history.log(slug, limit=limit)]}

    async def diff_agent(self, slug: str, from_commit: str = "", to_commit: str = "") -> Result:
        changes = await self._history.diff(slug, from_commit, to_commit)
        return {"changes": [_change(c) for c in changes]}

    # -- changing ------------------------------------------------------------------

    async def create_agent(
        self, slug: str, name: str, description: str, template: str = ""
    ) -> Result:
        record = await self._agents.create(slug, name, description, template, actor=self._actor)
        report = await self._agents.validate(slug)
        return {
            "slug": record.slug,
            "template": record.template,
            "files": [e.path for e in await self._files.tree(slug) if e.kind is FileKind.FILE],
            "diagnostics": [_diag(d) for d in report.diagnostics],
        }

    async def write_agent_file(self, slug: str, path: str, content: str) -> Result:
        self._writable_agent(slug)
        await self._agents.require(slug)
        async with edit_lock(self._queue, slug, wait=self._lock_wait):
            out = await self._files.write(slug, path, content.encode(), actor=self._actor)
        return {
            "path": out.result.entry.path,
            "size": out.result.entry.size,
            "diagnostics": [_diag(d) for d in out.diagnostics],
        }

    async def delete_agent_file(self, slug: str, path: str, recursive: bool = False) -> Result:
        self._writable_agent(slug)
        await self._agents.require(slug)
        async with edit_lock(self._queue, slug, wait=self._lock_wait):
            await self._files.delete(slug, path, recursive=recursive, actor=self._actor)
        return {"deleted": path}

    async def assign_credential_set(self, slug: str, name: str) -> Result:
        """Set agent.json ``credential_set`` (empty name: use the default)."""
        self._writable_agent(slug)
        await self._agents.require(slug)
        if name and name not in {c.name for c in await self._credentials.list()}:
            raise InvalidArgumentError(
                f"no credential set named {name!r}; call list_credential_sets"
            )
        async with edit_lock(self._queue, slug, wait=self._lock_wait):
            current = await self._files.read(slug, MANIFEST_FILE)
            try:
                manifest: object = json.loads(current.data)
            except ValueError as e:
                raise InvalidArgumentError(f"agent.json is not valid JSON: {e}") from e
            if not isinstance(manifest, dict):
                raise InvalidArgumentError("agent.json must be a JSON object")
            data: dict[str, Any] = dict(manifest)  # pyright: ignore[reportUnknownArgumentType]
            if name:
                data["credential_set"] = name
            else:
                data.pop("credential_set", None)
            out = await self._files.write(
                slug,
                MANIFEST_FILE,
                (json.dumps(data, indent=2) + "\n").encode(),
                if_match=current.etag,
                actor=self._actor,
            )
        return {
            "credential_set": name or "(default)",
            "diagnostics": [_diag(d) for d in out.diagnostics],
        }

    async def rollback_agent(self, slug: str, commit: str, path: str = "") -> Result:
        self._writable_agent(slug)
        restored = await self._history.rollback(slug, commit, self._actor, path)
        return {"commit": _commit(restored)}

    # -- testing ---------------------------------------------------------------------

    async def test_run_agent(
        self, slug: str, prompt: str, timeout_seconds: float = DEFAULT_TEST_TIMEOUT
    ) -> Result:
        """Queue one turn on the agent and wait for it (up to the timeout)."""
        self._writable_agent(slug)
        if not prompt.strip():
            raise InvalidArgumentError("prompt is empty")
        timeout = max(5.0, min(float(timeout_seconds or DEFAULT_TEST_TIMEOUT), MAX_TEST_TIMEOUT))
        title = TEST_TITLE_PREFIX + " ".join(prompt.split())[:60]
        conv = await self._conversations.create(slug, title)
        run = await self._conversations.send_message(conv.id, prompt)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            current = await self._conversations.get_run(run.id)
            if current.status.terminal:
                break
            await asyncio.sleep(self._poll)
        return await self.get_test_run(run.id)

    async def get_test_run(self, run_id: str) -> Result:
        run = await self._conversations.get_run(run_id)
        self._writable_agent(run.agent_slug)
        events = await self._events.read(run.conversation_id, 0, 10_000)
        mine = [e for e in events if e.run_id == run.id]
        replies = [
            str(e.data.get("text", ""))
            for e in mine
            if e.kind is EventKind.ASSISTANT_TEXT and not e.parent_tool_use_id
        ]
        results = [e for e in mine if e.kind is EventKind.RESULT]
        reply = str(results[-1].data.get("text", "")) if results else ""
        reply = reply or (replies[-1] if replies else "")
        tools = [str(e.data.get("name", "")) for e in mine if e.kind is EventKind.TOOL_CALL]
        failed_tools = sum(
            1 for e in mine if e.kind is EventKind.TOOL_RESULT and e.data.get("is_error")
        )
        notices = [
            str(e.data.get("message", ""))
            for e in mine
            if e.kind is EventKind.NOTICE and e.data.get("level") in ("error", "warning")
        ]
        out: Result = {
            "run_id": run.id,
            "conversation_id": run.conversation_id,
            "agent": run.agent_slug,
            "status": run.status.value,
            "reply": reply[:MAX_REPLY_CHARS],
            "tool_calls": tools,
            "failed_tool_calls": failed_tools,
            "cost_usd": run.usage.cost_usd,
            "num_turns": run.usage.num_turns,
        }
        if run.error:
            out["error"] = run.error
        if notices:
            out["notices"] = notices[:20]
        if not run.status.terminal:
            out["note"] = (
                "still running; call get_test_run later"
                if run.status is RunStatus.RUNNING
                else "still queued behind other work; call get_test_run later"
            )
        return out
