# Decision log

All decisions made by Hansa on 2026-11-07 during planning. Details: [architecture.md](architecture.md).

| # | Topic | Decision | Notes |
| --- | --- | --- | --- |
| 1 | Language and stack | Python for the platform core, API and workers; TypeScript for the web UI | Chosen for the long-run platform: agent runtimes, evals, memory/RAG and MCP tooling are Python-first |
| 2 | Client API | gRPC + protobuf (`platform.v1`), Envoy for gRPC-Web and JSON transcoding | Lets CLI and other interfaces integrate; no bidi streams so browsers work; workers stay behind the queue |
| 3 | Infrastructure | Postgres + Redis in v1, every dependency behind a port | No tight coupling to any technology |
| 4 | Background jobs | Separate asyncio worker service, custom Redis Streams `JobQueue` adapter | One run per agent via lease lock + per-agent FIFO; no automatic retries of agent runs |
| 5 | Deployment | Docker Compose on a Linux server; devcontainers locally; cloud later | |
| 6 | Users and auth | Single user, simple admin login behind `AuthProvider` | Extend to multi-user later |
| 7 | Isolation | SDK bubblewrap sandbox, no container per run | Worker container may run with the privileges bubblewrap needs |
| 8 | Credentials | Global credential sets with a default; per-agent selection in the UI | Per-user scoping later via `owner` field |
| 9 | Model providers | Claude (Anthropic, Bedrock, Vertex) and Anthropic-compatible APIs call directly; OpenAI, Grok and others via LiteLLM | Claude sets bypass LiteLLM |
| 10 | Task shape | Chat-style multi-turn conversations in v1 | |
| 11 | Concurrency | One run per agent at a time; subagents allowed | |
| 12 | Agent-to-agent | Not in v1; room reserved (`AgentRunner` port, `call_agent` slot) | |
| 13 | Approvals | Risky-tool approval wait can be switched on/off per agent and per conversation | |
| 14 | Network | Open internet by default; optional per-agent allowlist | |
| 15 | Repository | `D:\Work\my\agent-playground`, local git for now | GitHub repo to be added later |
