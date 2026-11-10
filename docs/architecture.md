# Architecture

Agent Playground is a workspace of Claude Code agents plus one plugin and one mod. Claude Code (terminal, desktop, IDE, Cowork) does all the running on the user's subscription; this repo adds only conventions, a builder, bookkeeping and a view. The earlier standalone platform (Python worker, gRPC API, web UI, bubblewrap isolation; Phases 1-4) was retired on 2026-11-10 and is in git history up to `f46538e`.

## Goals and non-goals

- Build agents with an agent; each agent has its own working directory.
- Plug in MCP servers, claude.ai connectors and generated code per agent.
- Show what exists, what can be done and what is running.
- Never pay API prices and never chase Claude releases: reuse native primitives so new features arrive for free.
- Extensible to a second brain (an agent with a memory MCP and a notes vault) and media tools (MCP servers).
- Not a goal: isolation between agents, multi-user access, a hosted service.

## Feature to native primitive

| Feature | Built from |
| --- | --- |
| Agent | A folder `agents/<name>/`: `CLAUDE.md` (role), `.claude/` (skills, subagents, commands, hooks, settings), `.mcp.json`, `tools/`, `workspace/`, `memory/`, plus `agent.json` metadata for the playground |
| Separate working directory | The agent folder is the session's cwd (Claude Code, Cowork, or `claude -p`); outside folders via `workdirs` → `--add-dir` |
| Agent builder | The hub session (repo root) with the `build-agent` skill; `agent-builder` subagent for unattended or parallel builds |
| MCP servers | Per-agent `.mcp.json`; `catalog/mcp/*.json` entries the builder copies in with `pg add-mcp` |
| Connectors | The user's claude.ai connectors, available in every surface when signed in; listed in `agent.json` `connectors` for display and instructions |
| Custom code | `tools/<tool>/` in the agent: a script wrapped by a skill, or a PEP 723 Python MCP server run with `uv run --script`; shared ones in `catalog/tools/` |
| Giving tasks | `delegate` skill → `pg run` → `claude -p --output-format stream-json` in the agent folder, optionally detached; resume with the run's session id |
| Visualization | `playground-hud` mod: pane, status band, toasts, `/playground`; `status` skill as the text fallback |
| Second brain (later) | An agent with the `memory` catalog server and a notes folder; nothing platform-specific |

## Components

### Workspace

`playground.json` marks the root and names the folders (`agents_dir`, `runs_dir`, `catalog_dir`, `template_dir`). Everything finds the root by walking up from the current directory, so the helpers work from the hub or from inside any agent.

The root `CLAUDE.md` is the hub's instructions. Claude Code also loads it in agent sessions (parent CLAUDE.md files load), so it opens with a scope line telling agents to ignore it. `claudeMdExcludes` can't express "this file, wherever the repo is cloned" because it matches absolute paths only.

### Plugin `playground`

Skills, each a slash command too:

- `build-agent`: interview (at most 3 questions), plan, confirm, scaffold with `pg new`, write CLAUDE.md, skills, subagents, MCP and tools, `pg check`, smoke-test with `pg run`, commit, report. `agent-spec.md` beside it is the folder contract.
- `add-tool`: generated code, as script + skill or as an MCP server from `mcp_server_template.py`.
- `add-mcp`: catalog entry, hand-written server, or claude.ai connector.
- `delegate`: start, follow, resume runs.
- `status`: agents, runs and catalog as text.

Subagent `agent-builder` follows `build-agent` without asking questions.

`scripts/pg.py` is a stdlib-only Python CLI used by all skills: `list`, `new`, `check`, `catalog`, `add-mcp`, `run [--background] [--resume]`, `runs`, `show`. Skills call it as `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"`.

### Runs

`pg run <agent> "<task>"` starts `claude -p <task> --output-format stream-json --verbose --permission-mode <agent.json permission_mode>` with cwd = the agent folder, plus `--model`, `--max-turns`, `--add-dir` per workdir, `--mcp-config <agent>/.mcp.json` when the agent has servers, and `--resume <session>` for follow-ups. `--background` re-launches the runner detached so the run outlives the calling session.

Each run gets `.playground/runs/<id>/` (id = `YYYYMMDD-HHMMSS-<agent>-<hex4>`, so names sort by time):

- `run.json`: `status` (`queued`, `running`, `done`, `failed`; `lost` is derived when the runner's pid is gone, POSIX only), `task`, `activity` (last tool call or text line), `session_id`, `cost_usd`, `num_turns`, timestamps, `error`.
- `events.jsonl`: the raw stream-json. `result.md`: the final text. `stderr.log`: CLI stderr when not empty. `runner.log`: the detached runner's own output.

The run uses the user's own Claude Code login, so it is billed to the subscription like any session.

### Mod `playground-hud`

A hooks module (`hooks/register.tsx`, `hooks/scan.ts`). On `session.start` it registers `/playground`, scans the workspace through `$.fs`, opens the pane when the session is inside a workspace, and starts a 3 s timer (runs every tick, agents and catalog every 5th). The scan goes into one `$.state` atom (`playground-hud.snapshot`, shape `snapshot-1`); it is written only when something changed, so idle redraws don't happen.

- **Pane** `playground`: ongoing and recent runs (status, agent, age, activity or error, Show), agents (description; skills, subagents, MCP servers, tools, connectors; Run task, Edit), catalog, New agent / Add MCP / Refresh. Buttons fill the prompt with the matching skill command rather than acting on their own.
- **Band** above the prompt while any run is queued or running.
- **Toast** when a run finishes.

`tests/hud.test.tsx` mounts the pane on all four surfaces against an in-memory workspace.

### Catalog

`catalog/mcp/<name>.json`: `name`, `description`, `tags`, `needs`, `server` (the `.mcp.json` entry). `${PLAYGROUND_ROOT}` in a server becomes the root's path relative to the agent folder when added, so agents keep working when the repo moves. `catalog/tools/media/server.py` is the playground's own photo and video MCP server (Pillow, ffmpeg).

## Trust and permissions

Claude Code ignores a project's `permissions.allow` (and asks before starting its `.mcp.json` servers) until the workspace is trusted. Trust is recorded per project root (the git root), so accepting the trust dialog once in the repo root covers every agent. Background runs can't answer prompts: a tool that isn't allowed is refused, which is why agents list their tools in `settings.json` and `pg run` passes `--mcp-config` explicitly.

Isolation is out of scope: agents run with the user's own permissions on the user's machine.

## Testing

- `python -m unittest discover -s tests -v`: `pg.py` against a fake `claude`, plus an end-to-end test that runs the real Claude Code CLI against `tests/fake_model_api.py` (a fake Messages API; `call-tool: <tool> <json>` in the prompt makes it call a tool) and checks the example agent resizes an image through its MCP server.
- `claude plugin validate plugins/<plugin>` for both plugins, `claude plugin test plugins/playground-hud` for the mod.

## Extending

- **Second brain**: build a `brain` agent with the `memory` catalog server, a notes folder in `workspace/`, and skills for capture, link and recall; other agents can be told to delegate to it.
- **Media**: add MCP servers for desktop editors to the catalog as they appear; the `media-editor` agent shows the pattern.
- **Agent-to-agent**: an agent can delegate with the same `pg run` (allow `Bash(python */pg.py run *)` in its settings).
