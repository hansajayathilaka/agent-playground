# Agent Playground hub

**Scope:** this file is for sessions started in the workspace root (the folder with `playground.json`). If your working directory is inside `agents/<name>/`, you are that agent: ignore this file and follow the agent's own CLAUDE.md.

This workspace holds many agents. Each agent is a Claude Code folder under `agents/` with its own CLAUDE.md, skills, subagents, MCP servers, tools and working files. In the hub you are the coordinator and the agent builder.

## What you do here

- **Build or change agents**: use the `playground:build-agent` skill (the user may type `/playground:build-agent`). For several agents at once, use the `playground:agent-builder` subagent.
- **Give work to agents**: use `playground:delegate`. Each run is a headless Claude Code session in the agent's own folder, on the user's own login, recorded under `.playground/runs/`.
- **Add capabilities**: `playground:add-mcp` (MCP servers, claude.ai connectors) and `playground:add-tool` (generated code).
- **Report**: `playground:status` lists agents, tools and runs. In Claude Code, `/playground` opens the live pane.

Prefer delegating a task to the agent that owns it over doing it yourself in the hub, so the work lands in that agent's folder and memory. Do small things (reading a result, renaming a file) directly.

## Layout

- `agents/<name>/`: agents. The folder contract is `plugins/playground/skills/build-agent/agent-spec.md`.
- `templates/agent/`: what a new agent starts from.
- `catalog/mcp/*.json`: MCP servers ready to add to an agent; `catalog/tools/`: shared code behind catalog servers.
- `plugins/playground/`: the plugin (skills, subagent, `scripts/pg.py`). `plugins/playground-hud/`: the Claude Code mod (pane and status band).
- `.playground/runs/`: run records, git-ignored.

## Changing the platform itself

When the user asks to change the plugin, the mod, the template or the catalog (not an agent):

- Read `docs/architecture.md` and `docs/decisions.md` first and update them in the same change when a design decision moves.
- Keep `pg.py` stdlib-only Python 3.10+ and cross-platform (the user is on Windows): no shell-only tricks, paths through `pathlib`.
- Tests: `python -m unittest discover -s tests -v` (the end-to-end test runs the real `claude` CLI against `tests/fake_model_api.py`; it needs `uv`).
- Plugins: `claude plugin validate plugins/playground` and `claude plugin validate plugins/playground-hud`; mod tests: `claude plugin test plugins/playground-hud`.
- Secrets never go into agent folders, run records or the repo.
