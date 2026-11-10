---
name: tune-permissions
description: Choose an agent's tool permissions in .claude/settings.json. Use when creating an agent or when a test run shows a tool being refused or overused.
---

1. List what the job needs: reading files, web search or fetch, running scripts in `tools/`, MCP tools.
2. Write `permissions.allow` with exactly those, for example `"WebFetch"`, `"WebSearch"`, `"Bash(python3 tools/*)"`, `"mcp__github"`.
3. Add `permissions.deny` entries for anything dangerous the agent never needs (`"Bash(curl *)"` for an agent that should only use an API through MCP).
4. Remember the platform already confines the agent: it can only write `memory/` and `workspace/`, sees no other agent, and reaches the network only through the platform proxy. Permissions are about keeping the agent on task, not about the sandbox.
5. Don't add hooks unless the user asks for them; if you do, the scripts go in `.claude/hooks/` and are read-only at run time.
6. Validate, and re-run the test that showed the problem.
