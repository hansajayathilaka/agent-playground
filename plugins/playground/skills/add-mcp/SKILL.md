---
name: add-mcp
description: Plug an MCP server or a claude.ai connector into an agent (photo or video editors, browsers, databases, SaaS apps). Use when the user wants an agent to reach an app or service, or asks what tools are available to add.
argument-hint: "<agent> <server, app or service>"
---

# Plug an MCP server or connector into an agent

Request: $ARGUMENTS

The helper CLI is `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"` (`pg` below).

## 1. Catalog first

Run `pg catalog`. If an entry fits, run `pg add-mcp <agent> <entry>`. It merges the server into `agents/<agent>/.mcp.json`. Then check the entry's `needs` (node, uv, ffmpeg...) are installed and tell the user if not.

## 2. A server that isn't in the catalog

- Find the server's documented install command (its README). Prefer official servers from the vendor or `modelcontextprotocol`.
- stdio: `{"command": "...", "args": [...], "env": {"KEY": "${KEY}"}}`. Remote: `{"type": "http", "url": "...", "headers": {...}}`.
- Write it into the agent's `.mcp.json`. If more than one agent could use it, also add `catalog/mcp/<name>.json` with `name`, `description`, `tags`, `needs` and `server`, so the builder can reuse it.
- Desktop apps with an MCP server (some photo/video editors ship one or have community servers) usually need the app running; say so in the agent's CLAUDE.md.
- Secrets never go in the folder: reference `${VAR}` and tell the user which variable to set.

## 3. claude.ai connectors

Connectors (Gmail, Google Drive, Notion, Slack, ...) are enabled on the user's claude.ai account and show up in Claude Code, Cowork and chat automatically when signed in. Nothing goes in `.mcp.json`. Instead:

- Add the connector's name to `connectors` in `agent.json`.
- In the agent's CLAUDE.md, say what to use it for.
- Tell the user to enable it at claude.ai, Settings, Connectors, if it isn't already.

## 4. Allow and verify

- Add `mcp__<server>__*` to `permissions.allow` in the agent's `.claude/settings.json` if the agent should use it without prompting (needed for background runs).
- From the agent folder run `claude mcp list` and confirm it connects.
- Run `pg check <agent>`.
