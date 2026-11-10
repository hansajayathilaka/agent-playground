---
name: add-mcp-server
description: Connect an agent to an external service through an MCP server entry in .mcp.json. Use when an agent needs a third-party API or tool server.
---

1. Read the agent's current `.mcp.json` with `read_agent_file`.
2. Prefer a remote server when the service has one:

   ```json
   { "mcpServers": { "github": { "type": "http", "url": "https://api.githubcopilot.com/mcp/",
     "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" } } } }
   ```

   Otherwise a stdio server, which runs inside the agent's sandbox:

   ```json
   { "mcpServers": { "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "workspace"] } } }
   ```

3. Secrets are always `${NAME}` references, never literal values. Tell the user which secret names to add.
4. Merge the new entry into the existing `mcpServers` object; don't drop other servers.
5. Allow the server's tools in `.claude/settings.json` (`"mcp__<server>"` allows all of them).
6. Add the server's host to `network.allowlist` in `agent.json` if the agent uses an allowlist.
7. `validate_agent`, then a `test_run_agent` that uses one of the server's tools.
