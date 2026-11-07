# Agent Playground: notes for coding agents

Read `docs/architecture.md` and `docs/decisions.md` before changing anything. They are the source of truth; update them in the same change when a design decision moves.

## Ground rules

- Core code (`platform/core`) imports only port interfaces. Concrete technology lives in `platform/adapters` and is wired in one composition root.
- Every port has a contract test suite; a new adapter must pass it.
- The API never runs agents. Work goes API → `JobQueue` → worker; output goes worker → `EventLog` → `WatchEvents`.
- `.proto` files in `proto/` are the API source of truth. Never break `platform.v1`; run `buf breaking` before committing proto changes.
- Every file path from a client or the Builder goes through `resolve_agent_path()`. Never accept raw paths.
- Secrets never go into agent folders, logs or events. The worker builds each run's env from scratch.
- Don't add bidirectional or client streaming RPCs (browsers can't use them).

## Tooling

- Python (uv workspace in `platform/`): `uv run ruff format platform`, `uv run ruff check platform`, `uv run pyright` (strict), `uv run pytest`.
- Web (`web/`, pnpm): `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`.
- Protobuf: Buf from `web/node_modules/.bin/buf`. After changing `proto/` or `playground_core/manifest.py`, run `scripts/generate.sh` and commit the output; CI fails on drift.
- Generated Python lives in `playground_proto.platform.v1`, never a top-level `platform` package (it would shadow the stdlib).
- Local dev: the devcontainer runs the compose stack (`deploy/compose.yaml` + `.devcontainer/compose.yaml`).
