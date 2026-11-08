"""AgentRunner on the Claude Agent SDK (Python).

One run = one ``ClaudeSDKClient`` session with ``cwd`` = the agent folder and
``setting_sources=["project"]``, so the SDK loads CLAUDE.md, .claude/ and
.mcp.json exactly as Claude Code would. Conversation turns resume the SDK
session by id. SDK messages are translated into platform events by
``Translator``; clients never see SDK shapes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ServerToolResultBlock, ServerToolUseBlock

from playground_core.events import EventKind
from playground_core.models import RunStatus, RunUsage
from playground_core.paths import resolve_agent_path
from playground_core.ports import EventSink, RunOutcome, RunRequest
from playground_worker.guard import make_pre_tool_use_hook
from playground_worker.launcher import DROP_VAR, REAL_CLI_VAR, bundled_cli

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 20_000
MAX_INPUT_JSON_CHARS = 20_000
INTERRUPT_GRACE_SECONDS = 15.0


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters]"


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    items: list[Any] = content if isinstance(content, list) else [content]  # pyright: ignore[reportUnknownVariableType]
    for item in items:
        if isinstance(item, dict) and item.get("type") == "text":  # pyright: ignore[reportUnknownMemberType]
            parts.append(str(item.get("text", "")))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        else:
            parts.append(json.dumps(item, default=str))
    return "\n".join(parts)


@dataclass
class Translator:
    """Turns SDK messages into (kind, data, parent_tool_use_id) events.

    Partial-message stream events carry the content block index of the API
    stream; final AssistantMessages may arrive one block at a time. Final
    blocks are matched back to their stream index (per message id, in order,
    by block type) so clients can replace streamed deltas with final text.
    """

    session_id: str = ""
    result: ResultMessage | None = None
    _stream_message: dict[str, str] = field(default_factory=dict[str, str])
    _stream_blocks: dict[str, list[tuple[int, str]]] = field(
        default_factory=dict[str, list[tuple[int, str]]]
    )
    _used: dict[str, set[int]] = field(default_factory=dict[str, set[int]])

    def _final_index(self, message_id: str, block_type: str, fallback: int) -> int:
        used = self._used.setdefault(message_id, set())
        for index, kind in self._stream_blocks.get(message_id, []):
            if kind == block_type and index not in used:
                used.add(index)
                return index
        used.add(fallback)
        return fallback

    def translate(self, msg: Any) -> list[tuple[EventKind, dict[str, Any], str]]:
        out: list[tuple[EventKind, dict[str, Any], str]] = []
        if isinstance(msg, StreamEvent):
            parent = msg.parent_tool_use_id or ""
            ev = msg.event
            etype = ev.get("type")
            if etype == "message_start":
                self._stream_message[parent] = str(ev.get("message", {}).get("id", ""))
            elif etype == "content_block_start":
                mid = self._stream_message.get(parent, "")
                block = ev.get("content_block", {})
                kind = "tool_use" if block.get("type") == "server_tool_use" else block.get("type")
                self._stream_blocks.setdefault(mid, []).append((int(ev.get("index", 0)), str(kind)))
            elif etype == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    mid = self._stream_message.get(parent, "")
                    out.append(
                        (
                            EventKind.TEXT_DELTA,
                            {
                                "message_id": mid,
                                "index": int(ev.get("index", 0)),
                                "text": delta["text"],
                            },
                            parent,
                        )
                    )
            return out

        if isinstance(msg, AssistantMessage):
            parent = msg.parent_tool_use_id or ""
            mid = msg.message_id or ""
            if msg.session_id:
                self.session_id = msg.session_id
            for pos, block in enumerate(msg.content):
                if isinstance(block, TextBlock):
                    idx = self._final_index(mid, "text", pos)
                    data = {"message_id": mid, "index": idx, "text": block.text}
                    out.append((EventKind.ASSISTANT_TEXT, data, parent))
                elif isinstance(block, ThinkingBlock):
                    idx = self._final_index(mid, "thinking", pos)
                    data = {"message_id": mid, "index": idx, "text": block.thinking}
                    out.append((EventKind.THINKING, data, parent))
                elif isinstance(block, ToolUseBlock | ServerToolUseBlock):
                    self._final_index(mid, "tool_use", pos)
                    raw = json.dumps(block.input, default=str)
                    data = {
                        "tool_use_id": block.id,
                        "name": str(block.name),
                        "input_json": _clip(raw, MAX_INPUT_JSON_CHARS),
                    }
                    out.append((EventKind.TOOL_CALL, data, parent))
                elif isinstance(block, ServerToolResultBlock):
                    data = {
                        "tool_use_id": block.tool_use_id,
                        "content": _clip(
                            json.dumps(block.content, default=str), MAX_TOOL_RESULT_CHARS
                        ),
                        "is_error": False,
                    }
                    out.append((EventKind.TOOL_RESULT, data, parent))
            if msg.error:
                out.append(
                    (
                        EventKind.NOTICE,
                        {"level": "error", "message": f"model error: {msg.error}"},
                        parent,
                    )
                )
            return out

        if isinstance(msg, UserMessage):
            parent = msg.parent_tool_use_id or ""
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        data = {
                            "tool_use_id": block.tool_use_id,
                            "content": _clip(
                                _tool_result_text(block.content), MAX_TOOL_RESULT_CHARS
                            ),
                            "is_error": bool(block.is_error),
                        }
                        out.append((EventKind.TOOL_RESULT, data, parent))
            return out

        if isinstance(msg, TaskStartedMessage):
            data = {
                "task_id": msg.task_id,
                "tool_use_id": msg.tool_use_id or "",
                "description": msg.description,
                "status": "started",
                "summary": "",
            }
            out.append((EventKind.SUBAGENT, data, msg.tool_use_id or ""))
            return out

        if isinstance(msg, TaskNotificationMessage):
            data = {
                "task_id": msg.task_id,
                "tool_use_id": msg.tool_use_id or "",
                "description": "",
                "status": str(msg.status),
                "summary": msg.summary,
            }
            out.append((EventKind.SUBAGENT, data, msg.tool_use_id or ""))
            return out

        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                sid = msg.data.get("session_id")
                if isinstance(sid, str) and sid:
                    self.session_id = sid
            return out

        if isinstance(msg, ResultMessage):
            self.result = msg
            self.session_id = msg.session_id or self.session_id
            usage = msg.usage or {}
            data = {
                "is_error": msg.is_error,
                "text": msg.result or "",
                "num_turns": msg.num_turns,
                "cost_usd": msg.total_cost_usd or 0.0,
                "duration_ms": msg.duration_ms,
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "stop_reason": msg.terminal_reason or msg.stop_reason or msg.subtype,
            }
            out.append((EventKind.RESULT, data, ""))
        return out

    def usage(self) -> RunUsage:
        r = self.result
        if r is None:
            return RunUsage()
        u = r.usage or {}
        return RunUsage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cost_usd=float(r.total_cost_usd or 0.0),
            num_turns=r.num_turns,
            duration_ms=r.duration_ms,
        )


async def _allow_all(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow:
    # The approvals switch (Phase 5) plugs in here. Platform guards run as
    # PreToolUse hooks before this and can still deny.
    return PermissionResultAllow()


def launcher_path() -> str:
    found = shutil.which("playground-claude-launcher", path=str(Path(sys.executable).parent))
    return found or "playground-claude-launcher"


class ClaudeAgentRunner:
    def __init__(
        self,
        agents_root: Path,
        sessions_root: Path,
        passthrough: list[str],
    ) -> None:
        self._agents_root = agents_root
        self._sessions_root = sessions_root
        self._passthrough = passthrough
        self._real_cli = bundled_cli()
        self._launcher = launcher_path()

    def run_env(self, request: RunRequest) -> dict[str, str]:
        """The run's whole environment, built from scratch."""
        home = self._sessions_root / request.agent_slug
        config = home / ".claude"
        tmp = home / "tmp"
        for d in (config, tmp):
            d.mkdir(parents=True, exist_ok=True)
        env: dict[str, str] = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(config),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        for name in self._passthrough:
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(request.env)
        # The SDK adds the worker's own environment underneath options.env;
        # the launcher drops every one of those names that we did not set.
        env[DROP_VAR] = ",".join(sorted(k for k in os.environ if k not in env))
        env[REAL_CLI_VAR] = self._real_cli
        return env

    def options(self, request: RunRequest) -> ClaudeAgentOptions:
        agent_dir, _ = resolve_agent_path(self._agents_root, request.agent_slug, allow_root=True)
        return ClaudeAgentOptions(
            cwd=str(agent_dir),
            cli_path=self._launcher,
            setting_sources=["project"],
            system_prompt={"type": "preset", "preset": "claude_code"},
            env=self.run_env(request),
            model=request.model,
            max_turns=request.max_turns,
            max_budget_usd=request.max_budget_usd,
            resume=request.resume_session_id or None,
            include_partial_messages=True,
            permission_mode="default",
            can_use_tool=_allow_all,
            hooks={"PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(agent_dir)])]},
            stderr=lambda line: log.debug("claude[%s]: %s", request.run_id, line[:500]),
        )

    async def run(self, request: RunRequest, emit: EventSink, cancel: asyncio.Event) -> RunOutcome:
        tr = Translator(session_id=request.resume_session_id)
        options = self.options(request)

        async def pump(client: ClaudeSDKClient) -> None:
            async for msg in client.receive_response():
                for kind, data, parent in tr.translate(msg):
                    await emit(kind, data, parent_tool_use_id=parent)

        cancelled = False
        try:
            async with ClaudeSDKClient(options) as client:
                await client.query(request.prompt)
                reader = asyncio.create_task(pump(client))
                stopper = asyncio.create_task(cancel.wait())
                try:
                    done, _ = await asyncio.wait(
                        {reader, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stopper in done and reader not in done:
                        cancelled = True
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(client.interrupt(), 10)
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(asyncio.shield(reader), INTERRUPT_GRACE_SECONDS)
                    if reader.done():
                        reader.result()
                finally:
                    for t in (reader, stopper):
                        t.cancel()
                    for t in (reader, stopper):
                        with contextlib.suppress(asyncio.CancelledError):
                            await t
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("SDK run %s failed", request.run_id)
            await emit(EventKind.NOTICE, {"level": "error", "message": _safe_error(exc)})
            return RunOutcome(tr.session_id, RunStatus.FAILED, _safe_error(exc), tr.usage())

        result = tr.result
        if cancelled:
            return RunOutcome(tr.session_id, RunStatus.CANCELLED, "cancelled", tr.usage())
        if result is None:
            return RunOutcome(tr.session_id, RunStatus.FAILED, "the agent exited without a result")
        if result.is_error:
            reason = "; ".join(result.errors or []) or result.subtype
            return RunOutcome(tr.session_id, RunStatus.FAILED, reason[:2000], tr.usage())
        return RunOutcome(tr.session_id, RunStatus.SUCCEEDED, "", tr.usage())


def _safe_error(exc: BaseException) -> str:
    # SDK errors can embed the CLI's stderr; keep it short and single-line.
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text[:500]
