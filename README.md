# Agent Playground

A platform for creating and running many Claude Agent SDK agents. Each agent lives in its own folder (CLAUDE.md, rules, memory, settings, MCP config, hooks, skills, subagents), runs in a background worker sandboxed to that folder, and streams its output to any client. A Builder agent creates and edits the other agents.

Status: planning complete (2026-11-07). Implementation has not started.

## Docs

- [Architecture](docs/architecture.md): components, agent folder layout, SDK usage, isolation, gRPC API, job runners, credentials, phases.
- [Decision log](docs/decisions.md): every decision made during planning.
- Living plan with comments: https://claude.ai/artifact/8DNvqYQD6jHoP95k6qPCPv

## Stack at a glance

- Python core, API (`grpc.aio`) and workers (asyncio) on the Claude Agent SDK
- TypeScript web UI; all clients use the protobuf/gRPC API through Envoy
- Postgres + Redis behind pluggable ports
- bubblewrap sandbox per run, Docker Compose deployment, devcontainer for local work
