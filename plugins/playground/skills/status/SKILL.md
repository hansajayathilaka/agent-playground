---
name: status
description: Show what this Agent Playground workspace has (agents with their skills, subagents, MCP servers, tools and connectors, plus the MCP catalog) and what is running or recently finished. Use when the user asks what agents or tools exist, what can be done, or what is going on.
---

# Workspace status

Run these with `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"` and summarize them for the user:

1. `list` for agents and their capabilities.
2. `runs --limit 10` for ongoing and recent work.
3. `catalog` only if the user asks what can be added.

Lead with anything running or failed, then the agents in a compact table, then one line of suggestions (an agent that could be built, or a failed run worth retrying). In Claude Code, mention that `/playground` opens the live pane.
