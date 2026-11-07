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

## Tooling (planned)

Python: uv, ruff, pyright strict, pytest. Protobuf: Buf. Local dev: the devcontainer runs the compose stack.
