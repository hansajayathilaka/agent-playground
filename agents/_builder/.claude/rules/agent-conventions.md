# Agent folder conventions

Each agent is one folder that the Claude Agent SDK loads as its whole context, exactly like a Claude Code project.

```
agent.json              platform manifest (schema below)
CLAUDE.md               the agent's instructions; may @import other files
.mcp.json               MCP servers: {"mcpServers": {...}}
.claude/settings.json   permissions, hooks, env (no secrets)
.claude/rules/*.md      extra instructions, usually @imported from CLAUDE.md
.claude/skills/<name>/SKILL.md   skills (frontmatter: name, description)
.claude/agents/<name>.md         subagents (frontmatter: name, description, optional tools, model)
.claude/commands/*.md            slash commands
tools/                  scripts or small MCP servers the agent runs (read-only at run time)
memory/                 the agent's long-term notes (writable at run time)
workspace/              task inputs and outputs (writable at run time)
```

At run time the agent can write only `memory/` and `workspace/`. Everything else is read-only to it, so put anything it must change between runs in `memory/`.

## agent.json

```json
{
  "schema_version": 1,
  "name": "Display name",
  "description": "What the agent does",
  "model": "optional model id; default comes from the credential set",
  "credential_set": "optional credential set name; default set if omitted",
  "tool_packs": [],
  "approvals": { "mode": "off | risky | all" },
  "limits": {
    "max_turns": 50,
    "max_budget_usd": 2.0,
    "timeout_seconds": 1800,
    "memory_mb": 4096,
    "cpus": 2,
    "max_processes": 512
  },
  "network": { "allowlist": ["example.com", "*.example.org"] }
}
```

Unknown keys are errors. An empty `network.allowlist` means any public HTTP(S) address; list hosts to restrict it. Raw TCP and SSH never work from a run.

## .claude/settings.json

```json
{
  "permissions": {
    "allow": ["WebFetch", "WebSearch", "Bash(python3 tools/*)"],
    "deny": ["Bash(rm -rf *)"]
  },
  "env": { "API_BASE": "https://api.example.com", "API_TOKEN": "${API_TOKEN}" }
}
```

## Skills and subagents

A skill is `.claude/skills/<name>/SKILL.md`:

```markdown
---
name: weekly-report
description: Write the weekly status report from workspace/notes/. Use when asked for a weekly report.
---

1. Read every file in workspace/notes/ from the last 7 days.
2. ...
```

The description decides when the model uses the skill, so say what it does and when to use it. A subagent in `.claude/agents/<name>.md` uses the same frontmatter plus optional `tools:` (comma-separated) and `model:`.

## Writing CLAUDE.md

- Start with the agent's job in one or two sentences.
- Then working rules: where inputs come from, where outputs go (`workspace/`), what to remember (`memory/MEMORY.md`), and what it must never do.
- Keep it under about 80 lines; move detail into rules or skills.
- End with `@memory/MEMORY.md` so the agent sees its notes.
