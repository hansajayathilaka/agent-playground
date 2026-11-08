"""Platform guard for the SDK's file tools (isolation layer 1).

A PreToolUse hook that resolves every path a file tool would touch and
denies it unless it stays inside the agent folder. Writes are further
limited to ``memory/`` and ``workspace/``; ``state/`` and ``.git/`` are never
readable. Platform hooks run alongside the agent's own hooks and a deny from
any hook wins, so an agent's settings can add rules but not remove these.

Bash, hook scripts and stdio MCP servers are processes; the bubblewrap jail
around the whole CLI (``jail.py``) confines them, and the file tools too.
This hook stays as the first layer: it gives the model a clear reason
instead of a bare EROFS/ENOENT, and it covers runs without the jail.
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


def _inside(root: str, path: str) -> bool:
    return os.path.commonpath([root, path]) == root


def _other_agent_denial(tool: str, agents_root: str, resolved: str) -> str | None:
    """Read access to another agent's folder (Builder only)."""
    parts = Path(os.path.relpath(resolved, agents_root)).parts
    if parts and parts[0] == ".trash":
        return f"{tool}: deleted agents are not readable"
    if len(parts) >= 2 and parts[1] in HIDDEN_DIRS:
        return f"{tool}: {parts[1]}/ is reserved for the platform"
    return None


def check_tool(
    tool: str,
    tool_input: Mapping[str, Any],
    agent_root: Path,
    readable_agents_root: Path | None = None,
) -> str | None:
    """Return a reason to deny, or None to let the call through.

    ``readable_agents_root`` (the Builder) also allows reads anywhere under
    the agents root except deleted agents and each agent's state/ and .git/.
    """
    write = tool in WRITE_TOOLS
    keys = WRITE_TOOLS.get(tool) or READ_TOOLS.get(tool)
    if keys is None:
        return None
    root = os.path.realpath(agent_root)
    others = os.path.realpath(readable_agents_root) if readable_agents_root else None
    for key in keys:
        value = tool_input.get(key)
        if value is None or value == "":
            continue  # optional path: the tool defaults to cwd (the agent folder)
        if not isinstance(value, str) or "\x00" in value:
            return f"{tool}: invalid {key}"
        candidate = value if os.path.isabs(value) else os.path.join(root, value)
        resolved = os.path.realpath(candidate)
        if not _inside(root, resolved):
            if not write and others is not None and _inside(others, resolved):
                reason = _other_agent_denial(tool, others, resolved)
                if reason is not None:
                    return reason
                continue
            return f"{tool}: {value} is outside this agent's folder"
        rel = Path(os.path.relpath(resolved, root))
        top = rel.parts[0] if rel.parts and rel.parts[0] != "." else ""
        if top in HIDDEN_DIRS:
            return f"{tool}: {top}/ is reserved for the platform"
        if write and top not in WRITABLE_DIRS:
            return f"{tool}: agents may only write under memory/ or workspace/"
    return None


def make_pre_tool_use_hook(agent_root: Path, readable_agents_root: Path | None = None) -> Any:
    async def hook(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        data: Mapping[str, Any] = input_data
        tool = str(data.get("tool_name", ""))
        tool_input: Mapping[str, Any] = data.get("tool_input") or {}
        reason = check_tool(tool, tool_input, agent_root, readable_agents_root)
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
