# Agent Playground

A platform for creating and running many Claude Agent SDK agents. Each agent lives in its own folder (CLAUDE.md, rules, memory, settings, MCP config, hooks, skills, subagents), runs in a background worker sandboxed to that folder, and streams its output to any client. A Builder agent creates and edits the other agents.

Status: Phase 1 (Foundations) implemented: create and edit agent folders from the browser. Runs, isolation and the Builder come in later phases.

## Quick start

```bash
cp deploy/.env.example deploy/.env      # set ADMIN_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
```

Open http://localhost:8080 and sign in as `admin`. Native gRPC clients use the same port (`grpcurl -plaintext localhost:8080 list`).

## Development

Open the repo in the devcontainer (it starts Postgres, Redis and Envoy), then:

```bash
uv sync && (cd web && pnpm install)
uv run playground-api                  # API on :50051 (env from the devcontainer)
(cd web && pnpm dev)                   # UI on :5173; Envoy on :8080 fronts both
uv run pytest                          # Python tests (contract suites use TEST_DATABASE_URL / TEST_REDIS_URL)
uv run ruff check platform && uv run pyright
(cd web && pnpm lint && pnpm typecheck && pnpm test)
scripts/generate.sh                    # after editing proto/ or agent.json schema
```

Without the devcontainer, `AGENT_INDEX_DRIVER=memory SESSION_DRIVER=memory AGENTS_ROOT=./data/agents TEMPLATES_ROOT=agents/templates ADMIN_PASSWORD=dev uv run playground-api` runs the API with no Postgres or Redis.

## Docs

- [Architecture](docs/architecture.md): components, agent folder layout, SDK usage, isolation, gRPC API, job runners, credentials, phases.
- [Decision log](docs/decisions.md): every decision made during planning.
- Living plan with comments: https://claude.ai/artifact/8DNvqYQD6jHoP95k6qPCPv

## Stack at a glance

- Python core, API (`grpc.aio`) and workers (asyncio) on the Claude Agent SDK
- TypeScript web UI; all clients use the protobuf/gRPC API through Envoy
- Postgres + Redis behind pluggable ports
- bubblewrap sandbox per run, Docker Compose deployment, devcontainer for local work
