---
name: create-agent
description: Create a new working agent from a description, test it and report back. Use whenever the user asks for a new agent.
---

1. **Understand the job.** Write one sentence: who the agent serves, its input, its output. Note what tools it needs (web, files, an API, an MCP server, scripts).
2. **Check what exists.** Call `list_agents`. If a close match exists, say so and offer to change it instead.
3. **Pick a slug and template.** Slug: short, lowercase, dashes (`invoice-checker`). Call `list_templates`; use `assistant` when the agent needs the web, otherwise `blank`.
4. **Create it.** `create_agent(slug, name, description, template)`.
5. **Write the instructions.** Replace `CLAUDE.md` (see the conventions rule). Add rules or skills only when they make the main file shorter or the behaviour more reliable. If the job has distinct repeatable procedures, make each one a skill.
6. **Set permissions.** Write `.claude/settings.json` with only the tools the job needs. Use the `tune-permissions` skill.
7. **Add integrations.** For an external service, use the `add-mcp-server` skill; secrets stay out of the folder.
8. **Limits.** Set `limits.max_turns` (20-60 for most agents) and a `network.allowlist` if the agent should only reach known hosts.
9. **Validate.** `validate_agent`; fix everything it reports.
10. **Test.** `test_run_agent` with a realistic prompt that exercises the main path. Check the reply, the tool calls and `failed_tool_calls`. If it failed, read the error, fix the folder, and test again (at most three rounds).
11. **Report.** Slug, one-line purpose, files written, test result (status and a short quote of the reply), and any action the user must take.
