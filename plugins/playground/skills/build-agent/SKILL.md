---
name: build-agent
description: Design and create a new agent (or change an existing one) in an Agent Playground workspace, as a Claude Code folder with its own CLAUDE.md, skills, subagents, MCP servers, tools and settings. Use when the user wants a new agent, asks to edit, extend or rename an agent, or describes a job that needs a dedicated agent.
argument-hint: "<what the agent should do> | edit <agent> <change>"
---

# Build an agent

You are the agent builder. An agent is a folder `agents/<name>/` that Claude Code runs with that folder as its working directory, so everything the agent knows and can do must be in the folder. Read [agent-spec.md](agent-spec.md) before writing files: it is the folder contract and the format of each piece.

The helper CLI is `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"` (call it `pg` below). Run it from anywhere inside the workspace. If `python` isn't found, try `python3` or `py`.

## 1. Understand the job

Request: $ARGUMENTS

- Run `pg list` to see the existing agents. If one already covers most of the job, suggest extending it instead of adding a near-duplicate.
- If the request is to edit an agent, read its whole folder first and change only what the request needs.
- Ask at most 3 short questions, only for what you can't sensibly default: what inputs it gets, what it should produce, and what outside systems it touches (apps, APIs, folders). Skip the questions if the request already answers them.

## 2. Design it, then confirm in one message

Write a short plan for the user:

- **Name** (lowercase, dashes) and a one-line description.
- **Role and rules** for CLAUDE.md: what it does, how it works, what "done" means, what it must never do.
- **Skills**: repeatable procedures it should follow (one per workflow).
- **Subagents**: only when a step is big, parallel, or needs a separate context (e.g. a reviewer).
- **MCP servers**: from `pg catalog`, or a new one (see the `add-mcp` skill). Name claude.ai connectors it expects (Gmail, Drive, Notion...) separately: those come from the user's account, not the folder.
- **Custom tools**: code it needs that no MCP covers (see the `add-tool` skill).
- **Workdirs**: outside folders it works on, if any.
- **Permissions**: `permission_mode` and allow rules. Default `acceptEdits` with the allow list from the template; isolation is out of scope here, so only tighten when the user asks.

Wait for the user's OK unless they said to go ahead.

## 3. Build it

1. `pg new <name> --description "<one line>"` copies `templates/agent/` to `agents/<name>/`.
2. Write `CLAUDE.md`: role, inputs and outputs, working rules, definition of done. Keep it under ~80 lines; move long procedures into skills.
3. Fill in `agent.json` (tags, workdirs, connectors, model if not default, max_turns).
4. Add each skill as `.claude/skills/<skill>/SKILL.md` and each subagent as `.claude/agents/<sub>.md`.
5. Add MCP servers with `pg add-mcp <name> <catalog-entry>`, or write `.mcp.json` entries by hand for servers not in the catalog.
6. Generate custom tools following the `add-tool` skill.
7. Extend `.claude/settings.json` `permissions.allow` with the tools the agent needs without prompting (e.g. `Bash(python tools/*)`, `mcp__media-tools__*`).
8. Run `pg check <name>` and fix everything it reports.
9. Smoke-test when it's cheap: `pg run <name> "<a tiny real task>"` and read the result. Skip if the task needs connectors or apps the run can't reach, and say so.
10. If the workspace is a git repo, commit the new folder: `git add agents/<name> && git commit -m "Add <name> agent"`.

## 4. Report

Tell the user in a few lines: what the agent does, its skills, MCP servers, tools and connectors, how to use it (`cd agents/<name> && claude`, open the folder in Cowork, or `/playground:delegate <name> <task>` from the workspace root), and anything they must set up themselves (a connector to enable, an API key, an app to install).
