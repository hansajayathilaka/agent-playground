# Agent folder contract

Every agent is a self-contained Claude Code project. Claude Code (terminal, desktop, Cowork) or `pg run` starts it with the agent folder as the working directory, so the folder alone decides what the agent knows and can do.

```
agents/<name>/
  CLAUDE.md                 role, rules, definition of done; imports memory
  agent.json                playground metadata (below); Claude Code ignores it
  .mcp.json                 MCP servers this agent can use
  .claude/
    settings.json           permissions, hooks, env, MCP approval
    skills/<skill>/SKILL.md procedures (plus any files they reference)
    agents/<sub>.md         subagents
    commands/<cmd>.md       optional slash commands (skills are preferred)
  tools/<tool>/             generated code: scripts or a local MCP server, each with README.md
  workspace/                task inputs and outputs
  memory/MEMORY.md          durable notes the agent maintains
```

Names are lowercase letters, digits and dashes. Never put secrets in the folder: API keys go in the user's environment or in `.claude/settings.local.json` (git-ignored), referenced as `${VAR}` in `.mcp.json`.

## agent.json

```json
{
  "description": "One line: what the agent does",
  "tags": ["media"],
  "status": "active",
  "permission_mode": "acceptEdits",
  "model": "",
  "max_turns": 50,
  "workdirs": ["D:/Photos/inbox"],
  "connectors": ["Google Drive"]
}
```

- `permission_mode`: what `pg run` passes to `claude -p --permission-mode` (`default`, `acceptEdits`, `plan`, `bypassPermissions`). Background runs can't answer prompts, so a tool that isn't allowed is simply refused; allow what the agent needs in `settings.json`, or use `bypassPermissions` when the user accepts that.
- `model`: empty for the user's default, or an alias like `sonnet`, `opus`, `haiku`.
- `workdirs`: extra folders the agent may work in (passed as `--add-dir`).
- `connectors`: claude.ai connectors the agent expects. They come from the user's account and are not configured in the folder; this list is for display and for the agent's own instructions.

## CLAUDE.md

Loaded at the start of every session. Write it in second person. Sections that work well: what you do, inputs, outputs (paths under `workspace/`), how you work (numbered steps or rules), done means, never. Keep the `@memory/MEMORY.md` import at the end.

The workspace root also has a CLAUDE.md (the hub's). Claude Code loads parent CLAUDE.md files too; the hub's file says it doesn't apply inside an agent folder, so don't repeat that.

## Skills: `.claude/skills/<skill>/SKILL.md`

```markdown
---
name: resize-for-web
description: Resize and compress photos for the web. Use when asked to prepare images for a site or blog.
argument-hint: "<folder or files>"
allowed-tools: mcp__media-tools__image_resize, mcp__media-tools__image_convert
---

Steps the agent follows. `$ARGUMENTS` is what the user typed after /resize-for-web.
Reference extra files in the skill folder by relative link, e.g. [checklist](checklist.md).
```

The `description` decides when Claude uses the skill on its own, so say what it does and when to use it. Add `disable-model-invocation: true` for skills that should only run when the user types the slash command.

## Subagents: `.claude/agents/<sub>.md`

```markdown
---
name: reviewer
description: Reviews a finished edit against the brief. Use after producing any final output.
tools: Read, Glob, Grep
model: inherit
---

System prompt for the subagent.
```

Leave `tools` out to inherit every tool. Subagents run inside the agent's session and folder.

## MCP servers: `.mcp.json`

```json
{
  "mcpServers": {
    "media-tools": { "command": "uv", "args": ["run", "--script", "../../catalog/tools/media/server.py"] },
    "my-tool": { "command": "uv", "args": ["run", "--script", "tools/my-tool/server.py"], "env": { "API_KEY": "${MY_TOOL_API_KEY}" } },
    "remote": { "type": "http", "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${REMOTE_TOKEN}" } }
  }
}
```

stdio servers start with the agent folder as their working directory, so relative paths are relative to it. The template's `settings.json` sets `enableAllProjectMcpServers` so the agent's own servers start without an approval prompt. Tool names are `mcp__<server>__<tool>`; allow them in settings with `mcp__<server>__*`.

## settings.json

```json
{
  "permissions": {
    "allow": ["Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch", "Bash(python tools/*)", "mcp__media-tools__*"],
    "deny": []
  },
  "enableAllProjectMcpServers": true,
  "env": { "SOME_NON_SECRET": "value" },
  "hooks": {
    "PostToolUse": [
      { "matcher": "Write|Edit", "hooks": [{ "type": "command", "command": "python tools/lint/check.py" }] }
    ]
  }
}
```

Use hooks for things that must always happen (format after edits, log outputs, block a command), not for things the model should decide.

## Tools: `tools/<tool>/`

Generated code the agent runs. See the `add-tool` skill. Each tool folder has a `README.md` (what it does, how to run it, inputs and outputs) and either a script the agent calls with Bash (wrapped by a skill) or an MCP server registered in `.mcp.json`.
