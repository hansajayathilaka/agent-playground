# Agent Playground

Build and run many agents on top of Claude Code, on your own Claude subscription. There is no server and no app to keep up to date: every agent is a plain Claude Code folder, so new Claude features (models, skills, connectors, tools) reach every agent the day they ship.

- **Agents are folders.** `agents/<name>/` holds the agent's CLAUDE.md, skills, subagents, MCP servers, generated tools, workspace and memory. Open the folder in Claude Code or Cowork and you are talking to that agent, in its own working directory.
- **An agent that builds agents.** `/playground:build-agent` interviews you, designs the agent, writes the folder, wires MCP servers and generates custom tools, checks it, smoke-tests it and commits it.
- **Plug anything in.** MCP servers per agent (`.mcp.json`, with a ready catalog including photo and video tools), claude.ai connectors from your account, and generated code under `tools/`.
- **Delegate in the background.** From the hub, `/playground:delegate <agent> <task>` runs `claude -p` in the agent's folder, records the run, and lets you follow it, resume it or start several in parallel.
- **See what's there and what's running.** The `playground-hud` mod adds a side pane (agents with their skills, MCP servers, tools and connectors; ongoing and recent runs; buttons to run, edit and add) and a status band while runs are going. `/playground:status` gives the same as text where mods don't draw (Cowork, chat).

## Set up (once)

Needs Claude Code 2.1 or newer signed in with your subscription, Python 3.10+, and [uv](https://docs.astral.sh/uv/) for MCP servers written in Python (ffmpeg too for the video tools).

```bash
git clone https://github.com/hansajayathilaka/agent-playground
cd agent-playground
claude plugin marketplace add ./      # the trailing slash matters; an absolute path works too
claude plugin install playground@agent-playground
claude plugin install playground-hud@agent-playground
claude          # accept the trust dialog once: agent settings and MCP servers only apply in a trusted workspace
```

The marketplace is this folder, so edits to the plugins apply after `/reload-plugins`. User scope (the default) makes the plugins load in the hub and in every agent folder.

## Use it

| You want to | Do this |
| --- | --- |
| Build an agent | In the repo root: `/playground:build-agent an agent that turns my voice memos into blog drafts` |
| Change an agent | `/playground:build-agent edit media-editor add a watermark tool` |
| Talk to one agent | `cd agents/media-editor && claude`, or open that folder in Cowork |
| Hand off a task | `/playground:delegate media-editor make web versions of everything in workspace/in` |
| Check on work | `/playground` (pane), or `/playground:status` |
| Add an MCP server or connector | `/playground:add-mcp media-editor playwright` |
| Generate a custom tool | `/playground:add-tool media-editor batch-rename photos by EXIF date` |

The same steps work without slash commands: ask in plain words and Claude picks the skill.

From a terminal the helper does the bookkeeping directly: `python plugins/playground/scripts/pg.py list | runs | show <id> | run <agent> "<task>" --background`.

### Where each Claude surface fits

- **Claude Code (terminal, desktop, IDE)**: everything, including the pane and background runs.
- **Cowork**: open an agent folder (or the hub) as the working folder; skills, MCP servers and connectors work, the pane may not draw, so use `/playground:status`.
- **Claude chat**: no folders. Upload a skill (for example an agent's skill folder, zipped) and use connectors there; keep the agent's source of truth in the repo.

## Layout

```
playground.json            marks the workspace root
CLAUDE.md                  hub instructions (the coordinator and builder)
agents/<name>/             one folder per agent (see agent-spec.md)
templates/agent/           starting point for new agents
catalog/mcp/*.json         MCP servers ready to add; catalog/tools/ holds shared server code
plugins/playground/        plugin: build-agent, add-tool, add-mcp, delegate, status skills, agent-builder subagent, scripts/pg.py
plugins/playground-hud/    Claude Code mod: pane, status band, /playground
tests/                     python -m unittest discover -s tests
docs/                      architecture and decisions
```

The agent folder contract is [plugins/playground/skills/build-agent/agent-spec.md](plugins/playground/skills/build-agent/agent-spec.md). Design notes: [docs/architecture.md](docs/architecture.md), [docs/decisions.md](docs/decisions.md).

The earlier standalone platform (Python worker, gRPC API, web UI, bubblewrap isolation) is in git history up to commit `f46538e`.
