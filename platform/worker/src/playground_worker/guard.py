"""Platform guard for the SDK's file tools (isolation layer 1).

A PreToolUse hook that resolves every path a file tool would touch and
denies it unless it stays inside the agent folder. Writes are further
limited to ``memory/`` and ``workspace/``; ``state/`` and ``.git/`` are never
readable. Platform hooks run alongside the agent's own hooks and a deny from
any hook wins, so an agent's settings can add rules but not remove these.

This does not cover Bash, hook scripts or stdio MCP servers: those are
processes, and contain them is the job of the OS sandbox (Phase 3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

READ_TOOLS: Mapping[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Glob": ("path",),
    "Grep": ("path",),
    "LS": ("path",),
}
WRITE_TOOLS: Mapping[str, tuple[str, ...]] = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}
HIDDEN_DIRS = frozenset({"state", ".git"})
WRITABLE_DIRS = frozenset({"memory", "workspace"})


def check_tool(tool: str, tool_input: Mapping[str, Any], agent_root: Path) -> str | None:
    """Return a reason to deny, or None to let the call through."""
    write = tool in WRITE_TOOLS
    keys = WRITE_TOOLS.get(tool) or READ_TOOLS.get(tool)
    if keys is None:
        return None
    root = os.path.realpath(agent_root)
    for key in keys:
        value = tool_input.get(key)
        if value is None or value == "":
            continue  # optional path: the tool defaults to cwd (the agent folder)
        if not isinstance(value, str) or "\x00" in value:
            return f"{tool}: invalid {key}"
        candidate = value if os.path.isabs(value) else os.path.join(root, value)
        resolved = os.path.realpath(candidate)
        if os.path.commonpath([root, resolved]) != root:
            return f"{tool}: {value} is outside this agent's folder"
        rel = Path(os.path.relpath(resolved, root))
        top = rel.parts[0] if rel.parts and rel.parts[0] != "." else ""
        if top in HIDDEN_DIRS:
            return f"{tool}: {top}/ is reserved for the platform"
        if write and top not in WRITABLE_DIRS:
            return f"{tool}: agents may only write under memory/ or workspace/"
    return None


def make_pre_tool_use_hook(agent_root: Path) -> Any:
    async def hook(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        data: Mapping[str, Any] = input_data
        tool = str(data.get("tool_name", ""))
        tool_input: Mapping[str, Any] = data.get("tool_input") or {}
        reason = check_tool(tool, tool_input, agent_root)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook
