"""The Builder's platform tools as an in-process SDK MCP server.

The server lives in the worker process, so tool calls run outside the jail
with the worker's access to the agents volume. Each tool is a thin wrapper
over ``BuilderToolkit``; domain errors come back as tool errors the model
can read and fix. The model sees them as ``mcp__platform__<tool>``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig
from mcp.types import ToolAnnotations

from playground_core.errors import DomainError
from playground_core.services.builder import BuilderToolkit

log = logging.getLogger(__name__)

SERVER_NAME = "platform"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


SLUG = _str("Agent slug (folder name), for example 'news-digest'")
PATH = _str("Path relative to the agent folder, for example '.claude/skills/x/SKILL.md'")


def _wrap(call: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> Handler:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await call(args)
        except DomainError as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "content": [{"type": "text", "text": f"Error: bad arguments ({exc})"}],
                "is_error": True,
            }
        except Exception:
            log.exception("builder tool failed")
            return {
                "content": [{"type": "text", "text": "Error: internal platform error"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}]}

    return handler


def builder_tools(kit: BuilderToolkit) -> list[SdkMcpTool[Any]]:
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False)
    destructive = ToolAnnotations(read_only_hint=False, destructive_hint=True)

    def t(
        name: str,
        description: str,
        schema: dict[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        annotations: ToolAnnotations,
    ) -> SdkMcpTool[Any]:
        return SdkMcpTool(name, description, schema, _wrap(call), annotations)

    return [
        t(
            "list_agents",
            "List every agent (slug, name, description), except the Builder itself.",
            _schema({}, []),
            lambda a: kit.list_agents(),
            read,
        ),
        t(
            "list_templates",
            "List agent templates and the files each one starts with.",
            _schema({}, []),
            lambda a: kit.list_templates(),
            read,
        ),
        t(
            "list_credential_sets",
            "List credential sets by name (no secrets). An agent uses the default set "
            "unless agent.json names another one.",
            _schema({}, []),
            lambda a: kit.list_credential_sets(),
            read,
        ),
        t(
            "get_agent",
            "Show one agent: agent.json, its file list, validation diagnostics and recent changes.",
            _schema({"slug": SLUG}, ["slug"]),
            lambda a: kit.get_agent(a["slug"]),
            read,
        ),
        t(
            "read_agent_file",
            "Read a text file from an agent folder.",
            _schema({"slug": SLUG, "path": PATH}, ["slug", "path"]),
            lambda a: kit.read_agent_file(a["slug"], a["path"]),
            read,
        ),
        t(
            "create_agent",
            "Create a new agent folder from a template. Slugs use lowercase letters, digits "
            "and dashes. Fill in CLAUDE.md and the other files afterwards.",
            _schema(
                {
                    "slug": SLUG,
                    "name": _str("Display name"),
                    "description": _str("One or two sentences on what the agent does"),
                    "template": _str("Template name from list_templates (default 'blank')"),
                },
                ["slug", "name", "description"],
            ),
            lambda a: kit.create_agent(
                a["slug"], a["name"], a["description"], a.get("template", "")
            ),
            write,
        ),
        t(
            "write_agent_file",
            "Create or replace a text file in an agent folder (not state/ or .git/). Returns "
            "validation diagnostics for that file. Every write is recorded in the agent's "
            "history.",
            _schema(
                {"slug": SLUG, "path": PATH, "content": _str("Full new file content")},
                ["slug", "path", "content"],
            ),
            lambda a: kit.write_agent_file(a["slug"], a["path"], a["content"]),
            write,
        ),
        t(
            "delete_agent_file",
            "Delete a file, or a folder with recursive=true, from an agent folder. agent.json "
            "can't be deleted.",
            _schema(
                {"slug": SLUG, "path": PATH, "recursive": {"type": "boolean"}},
                ["slug", "path"],
            ),
            lambda a: kit.delete_agent_file(a["slug"], a["path"], bool(a.get("recursive"))),
            destructive,
        ),
        t(
            "validate_agent",
            "Validate the whole agent folder. runnable=false means errors block runs.",
            _schema({"slug": SLUG}, ["slug"]),
            lambda a: kit.validate_agent(a["slug"]),
            read,
        ),
        t(
            "assign_credential_set",
            "Make the agent use a credential set by name (empty name: the default set).",
            _schema({"slug": SLUG, "name": _str("Credential set name")}, ["slug", "name"]),
            lambda a: kit.assign_credential_set(a["slug"], a["name"]),
            write,
        ),
        t(
            "agent_history",
            "List recent changes to an agent folder, newest first.",
            _schema({"slug": SLUG, "limit": {"type": "integer"}}, ["slug"]),
            lambda a: kit.agent_history(a["slug"], int(a.get("limit") or 20)),
            read,
        ),
        t(
            "diff_agent",
            "Show what changed between two commits of an agent (default: the latest change).",
            _schema(
                {
                    "slug": SLUG,
                    "from_commit": _str("Older commit id (default: parent of to_commit)"),
                    "to_commit": _str("Newer commit id (default: latest)"),
                },
                ["slug"],
            ),
            lambda a: kit.diff_agent(a["slug"], a.get("from_commit", ""), a.get("to_commit", "")),
            read,
        ),
        t(
            "rollback_agent",
            "Restore an agent folder, or one path in it, to an earlier commit. The rollback "
            "is itself a new commit, so it can be undone.",
            _schema(
                {"slug": SLUG, "commit": _str("Commit id from agent_history"), "path": PATH},
                ["slug", "commit"],
            ),
            lambda a: kit.rollback_agent(a["slug"], a["commit"], a.get("path", "")),
            destructive,
        ),
        t(
            "test_run_agent",
            "Run the agent once on a test prompt and wait for its reply (up to "
            "timeout_seconds, default 180). Uses the agent's own credentials and sandbox. "
            "Returns status, reply, tool calls and errors.",
            _schema(
                {
                    "slug": SLUG,
                    "prompt": _str("A realistic task for the agent"),
                    "timeout_seconds": {"type": "number"},
                },
                ["slug", "prompt"],
            ),
            lambda a: kit.test_run_agent(
                a["slug"], a["prompt"], float(a.get("timeout_seconds") or 0)
            ),
            write,
        ),
        t(
            "get_test_run",
            "Check a test run started by test_run_agent that had not finished yet.",
            _schema({"run_id": _str("run_id from test_run_agent")}, ["run_id"]),
            lambda a: kit.get_test_run(a["run_id"]),
            read,
        ),
    ]


def builder_server(kit: BuilderToolkit) -> McpSdkServerConfig:
    return create_sdk_mcp_server(SERVER_NAME, tools=builder_tools(kit))
