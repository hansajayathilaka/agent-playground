# Decision log

Decisions 1-41 belonged to the standalone platform (Phases 1-4) and are in git history (`docs/decisions.md` at `f46538e`, and the Phase 4 branch). On 2026-11-10 Hansa retired that platform in favour of building on Claude Code itself. Decisions 42-43 are Hansa's; 44-55 were made while building the first version and are open for review.

| # | Topic | Decision | Notes |
| --- | --- | --- | --- |
| 42 | Platform | Build on Claude Code, Cowork and Claude chat instead of a standalone app | Subscription pricing instead of API pricing; new Claude features arrive without platform work (Hansa, 2026-11-10) |
| 43 | Isolation | Out of scope | Agents run as the user on the user's machine (Hansa, 2026-11-10) |
| 44 | Agent shape | An agent is a Claude Code project folder `agents/<name>/` plus `agent.json` metadata | Every native feature works in it unchanged; `agent.json` is the only playground-specific file |
| 45 | Distribution | One repo is the workspace and a local plugin marketplace with two plugins: `playground` (skills, subagent, helper) and `playground-hud` (mod) | Installed at user scope so both load in the hub and every agent folder. The mod is separate so it can be switched off on its own, and so the skills plugin keeps working where mods don't load (Cowork, chat uploads) |
| 46 | Builder | The hub session runs the `build-agent` skill; an `agent-builder` subagent exists for unattended builds | No separate builder agent folder: the builder needs to see every agent, which the hub does |
| 47 | Helper | `pg.py`: stdlib-only Python 3.10+ CLI shared by all skills | Hansa is on Windows; Python is more portable there than shell. No dependencies to install |
| 48 | Delegation | `claude -p --output-format stream-json` in the agent folder, optionally detached; records in `.playground/runs/<id>/` | Uses the user's login. Runs survive the calling session. Resume via the session id |
| 49 | Headless MCP | `pg run` passes `--mcp-config <agent>/.mcp.json` | Project `.mcp.json` servers wait for an interactive approval that a headless run can't give |
| 50 | Trust | Accept Claude Code's trust dialog once in the repo root | Untrusted workspaces ignore project `permissions.allow`; trust is per git root, so one acceptance covers all agents |
| 51 | Hub CLAUDE.md | Root CLAUDE.md is the hub's, opening with a scope line telling agent sessions to ignore it | Parent CLAUDE.md files always load; `claudeMdExcludes` only takes absolute paths |
| 52 | Generated tools | Python, PEP 723 inline dependencies, run with `uv run --script`; MCP servers pin `mcp<2` | No virtualenvs to manage; mcp 2.x renamed FastMCP, which broke the first version |
| 53 | Catalog paths | `${PLAYGROUND_ROOT}` in catalog servers becomes a path relative to the agent folder | Agents keep working when the repo is cloned to another path |
| 54 | Agent data in git | Agent folders are committed; `workspace/` contents and `settings.local.json` are ignored | Media inputs and outputs are large and personal; secrets go in local settings or the environment |
| 55 | HUD actions | Pane buttons fill the prompt with a skill command instead of acting directly | The user sees and can edit what will run; the skills stay the single way to do each thing |
