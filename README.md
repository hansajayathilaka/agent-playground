# Agent Playground

A platform for creating and running many Claude Agent SDK agents. Each agent lives in its own folder (CLAUDE.md, rules, memory, settings, MCP config, hooks, skills, subagents), runs in a background worker sandboxed to that folder, and streams its output to any client. A Builder agent creates and edits the other agents.

Status: Phases 1-3 implemented: create and edit agent folders, add model credentials, and chat with agents in the browser (background runs, live streaming, multi-turn sessions, cancel). Every run is jailed with bubblewrap to its own folder, with filtered network egress and per-run resource limits. The Builder (Phase 4) comes next. The worker container runs privileged; on Ubuntu 24.04+ hosts set `sysctl kernel.apparmor_restrict_unprivileged_userns=0` (see docs/architecture.md, "Phase 3 as built").

## Quick start

```bash
cp deploy/.env.example deploy/.env      # set ADMIN_PASSWORD and SECRETS_KEY
docker compose -f deploy/compose.yaml up -d --build
```

Open http://localhost:8080, sign in as `admin`, add a credential set under Credentials, then open an agent's Chat tab. Native gRPC clients use the same port (`grpcurl -plaintext localhost:8080 list`).

## Development

Open the repo in the devcontainer (it starts Postgres, Redis and Envoy), then:

```bash
uv sync && (cd web && pnpm install)
uv run playground-api                  # API on :50051 (env from the devcontainer)
uv run playground-worker               # runs agents (needs Redis + Postgres)
(cd web && pnpm dev)                   # UI on :5173; Envoy on :8080 fronts both
uv run pytest                          # Python tests (contract suites use TEST_DATABASE_URL / TEST_REDIS_URL)
uv run ruff check platform && uv run pyright
(cd web && pnpm lint && pnpm typecheck && pnpm test)
scripts/generate.sh                    # after editing proto/ or agent.json schema
```

Without the devcontainer, `DATABASE_DRIVER=memory REDIS_DRIVER=memory SECRETS_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= AGENTS_ROOT=./data/agents TEMPLATES_ROOT=agents/templates ADMIN_PASSWORD=dev uv run playground-api` runs the API with no Postgres or Redis (file editing only: runs need a worker sharing Redis).

## Docs

- [Architecture](docs/architecture.md): components, agent folder layout, SDK usage, isolation, gRPC API, job runners, credentials, phases.
- [Decision log](docs/decisions.md): every decision made during planning.
- Living plan with comments: https://claude.ai/artifact/8DNvqYQD6jHoP95k6qPCPv

## Stack at a glance

- Python core, API (`grpc.aio`) and workers (asyncio) on the Claude Agent SDK
- TypeScript web UI; all clients use the protobuf/gRPC API through Envoy
- Postgres + Redis behind pluggable ports
- bubblewrap sandbox per run, Docker Compose deployment, devcontainer for local work
