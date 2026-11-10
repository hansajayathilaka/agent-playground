---
name: add-tool
description: Generate custom code an agent can run (a script wrapped by a skill, or a small local MCP server) and wire it into the agent's folder. Use when an agent needs a capability no catalog MCP server or connector provides, or the user asks to "make a tool" for an agent.
argument-hint: "<agent> <what the tool should do>"
---

# Add a custom tool to an agent

Request: $ARGUMENTS

The helper CLI is `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"` (`pg` below). Agent folders follow [agent-spec.md](../build-agent/agent-spec.md).

## Pick the shape

- **Script + skill** when the agent runs it occasionally, the inputs are files or a few flags, and plain text output is fine. Simplest to write and debug.
- **MCP server** when the tool has several operations, structured inputs and outputs, needs to keep a connection or state between calls, or should be reusable by other agents (then put it in `catalog/tools/<tool>/` with a `catalog/mcp/<tool>.json` entry instead of in one agent).

Prefer Python with only the standard library. When you need packages, use a [PEP 723](https://peps.python.org/pep-0723/) header and run with `uv run --script`, so no virtualenv has to be managed. Check `uv --version` first; if uv is missing, tell the user (`pip install uv` or https://docs.astral.sh/uv/) rather than installing packages globally.

## Script + skill

1. Write `agents/<agent>/tools/<tool>/<tool>.py` with argparse, clear `--help`, exit code 0 on success, errors to stderr.
2. Write `tools/<tool>/README.md`: purpose, usage line, example, inputs, outputs.
3. Write `.claude/skills/<tool>/SKILL.md` telling the agent when and how to call it (`python tools/<tool>/<tool>.py ...`, run from the agent folder).
4. Allow it in `.claude/settings.json`: `"Bash(python tools/<tool>/*)"` (or `uv run --script tools/<tool>/*`).

## MCP server

1. Copy [mcp_server_template.py](mcp_server_template.py) to `tools/<tool>/server.py`, rename the server, and replace the example tool. One function per operation, typed parameters, a docstring that says what it does: those become the tool's schema and description.
2. Write `tools/<tool>/README.md`.
3. Register it in the agent's `.mcp.json`: `"<tool>": {"command": "uv", "args": ["run", "--script", "tools/<tool>/server.py"]}`. Secrets go in `env` as `${VAR}` references, never literal values.
4. Allow `mcp__<tool>__*` in `.claude/settings.json`.

## Test before you hand it over

- Script: run it once on a small real input from the agent folder.
- MCP server: from the agent folder run `claude mcp list` and check the server shows as connected, then call one tool in a quick `pg run <agent> "<use the tool on a sample>"`.
- Run `pg check <agent>`.

Report what you added, how the agent uses it, and any setup the user must do (install ffmpeg, set an env var).
