# Decision log

Decisions 1-15 were made by Hansa on 2026-11-07 during planning. Decisions 16-20 were implementation choices made in Phase 1, and 21-27 in Phase 2; they are open for review. Details: [architecture.md](architecture.md).

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
| 15 | Repository | GitHub `hansajayathilaka/agent-playground`, default branch `master` | Development happens in cloud environments; changes land via PRs |
| 16 | Web tooling | pnpm, Vite, React 19, TypeScript strict, Biome, Vitest; Connect-ES clients with the gRPC-Web transport; Monaco bundled locally | Made during Phase 1 implementation (2026-11-07) |
| 17 | Python proto package | Generated code lives in `playground_proto.platform.v1` (imports rewritten by `scripts/generate.sh`) | A top-level `platform` package would shadow the stdlib `platform` module. The protobuf package stays `platform.v1` |
| 18 | `WriteFile` shape | Unary with etag (`if_match`) and `create_only`, 3 MiB cap | The plan said "stream", but client streaming is banned for browser support. Larger files get a chunked-upload flow later |
| 19 | Login sessions | Opaque random bearer tokens; the `SessionStore` keeps only a SHA-256 of each token (Redis with TTL in v1); admin password from `ADMIN_PASSWORD` | Keeps `AuthProvider` swappable for OIDC later |
| 20 | Envoy routing | One listener on :8080: `/platform.v1.*` and `/grpc.*` go to the API, everything else to the web UI | Same origin, so no CORS; native gRPC clients use the same port |
| 21 | Queue design | Per-agent Redis list + `wake` stream (consumer group) as hints + Lua-checked lease lock; a job whose holder died returns once as `recovered` and is marked `interrupted` | Gives per-agent FIFO without the promote race in "defer to pending list"; addresses the review's lock-release concern |
| 22 | Event log | One Redis stream per conversation, entry id `0-<offset>` from an INCR counter; `WatchEvents` keyed by conversation | Clients resume with a plain integer offset. Postgres archiving deferred to Phase 6 |
| 23 | Event envelope | Typed `oneof` in `event.proto`; core event kinds are named exactly like the oneof fields | Clients get typed events; adding a kind = core enum + proto field |
| 24 | Run env | `cli_path` = `playground-claude-launcher`, which strips the worker's own env before exec'ing the bundled CLI | The SDK always inherits `os.environ`; this is also where Phase 3's outer sandbox will go |
| 25 | File-tool guard in Phase 2 | Platform `PreToolUse` hook confines file tools to the agent folder and writes to `memory/` + `workspace/` | Planned for Phase 3, pulled forward because runs became real. Bash, hooks and stdio MCP stay uncontained until Phase 3 |
| 26 | Secrets | AES-256-GCM, `SECRETS_KEY` (base64 32 bytes) in API and worker env, record id as associated data | Write-only through the API; only the worker decrypts |
| 27 | Settings | `DATABASE_DRIVER` / `REDIS_DRIVER` replace Phase 1's `AGENT_INDEX_DRIVER` / `SESSION_DRIVER` | One switch per backend now that more ports share them |
