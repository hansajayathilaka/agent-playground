# Agent Playground: architecture

Status: final plan, 2026-11-07. Phases 1 (Foundations), 2 (Runs) and 3 (Isolation) implemented; see the "as built" sections below.
Living copy with comments: https://claude.ai/artifact/8DNvqYQD6jHoP95k6qPCPv
Decision log: [decisions.md](decisions.md)

## Summary

Each agent is a folder on disk that the Claude Agent SDK loads as its whole context. A background worker runs each task with that folder as its `cwd`, inside a bubblewrap sandbox that can't see anything outside it. Events stream to clients through an event log. A privileged Builder agent creates and edits the other agents' folders.

Goals:

- One folder per agent: CLAUDE.md, rules, memory, settings, state, MCP config, hooks, skills, subagents.
- Every agent runs on the Claude Agent SDK (Python).
- An agent can only reach its own folder.
- Full view / create / edit / delete of any agent's files from the UI.
- Tasks run in background workers, never inside a request, and output streams live to clients.
- The platform core is separate from any interface (web UI, CLI, others) and reached through one gRPC API.
- A Builder agent can create and change any agent.
- Pluggable tools (web fetch, custom MCP servers, custom code, bash) and pluggable infrastructure.

## Architecture

```
            gRPC-Web            gRPC (unary + server stream)
 Web UI  ─────────────┐     ┌──────────────── CLI / other clients
 (TypeScript)         ▼     ▼
                   ┌─────────────┐
                   │    Envoy    │  gRPC-Web + optional JSON transcoding
                   └──────┬──────┘
                          ▼
                 ┌──────────────────┐   enqueue / read events   ┌──────────────────┐
                 │  API (Python,    │ ────────────────────────▶ │ Postgres + Redis │
                 │  grpc.aio)       │                           │ runs, queue,     │
                 └────────┬─────────┘                           │ event log,       │
                          │ file I/O                            │ control channel  │
                          ▼                                     └────────┬─────────┘
                 ┌──────────────────┐   platform MCP tools               │ jobs ▼  ▲ events
  Builder run ──▶│ Storage service  │                           ┌────────┴─────────┐
                 │ path guard, git  │                           │ Worker pool      │
                 └────────┬─────────┘                           │ (Python asyncio) │
                          ▼                                     └────────┬─────────┘
                 ┌──────────────────┐   own folder only        ┌─────────┴─────────┐
                 │ agents/<slug>/   │ ◀──────────────────────── │ Sandboxed run     │
                 └──────────────────┘                           │ SDK, cwd = folder │
                                                                └───────────────────┘
```

The API never runs an agent, and workers never serve client traffic. The API writes files through the storage service and queues runs. Workers run each turn in a sandbox scoped to that agent's folder and append events to the event log, which clients read through `WatchEvents`.

## Pluggable infrastructure

Every external dependency sits behind a port (interface) in the core package. Swapping one means writing an adapter and changing config.

| Port | Responsibility | v1 adapter | Possible later adapters |
| --- | --- | --- | --- |
| `JobQueue` | Enqueue, claim, ack, retry, per-agent lock | Redis Streams | Postgres queue, SQS, NATS |
| `EventLog` | Append run/chat events, replay from offset, tail live | Redis Streams (+ Postgres history) | Postgres + LISTEN/NOTIFY, Kafka, NATS JetStream |
| `ControlChannel` | Cancel, approval answers to a running worker | Redis pub/sub | Postgres NOTIFY, NATS |
| Repositories | Runs, conversations, credential sets, agent index | Postgres | SQLite for tests, MySQL |
| `AgentStore` | File CRUD inside one agent root, path guard | Local filesystem volume | S3/GCS-backed store, NFS |
| `SecretStore` | Encrypt/decrypt credentials and tool secrets | Postgres + AES-GCM, master key from env | Vault, AWS/GCP KMS |
| `Sandbox` | Wrap a run's processes | Outer bubblewrap jail around the CLI, in the Claude runner (decision 28) | Container per run, Firecracker |
| `ModelProvider` | Turn a credential set into SDK env / gateway config | Anthropic, Anthropic-compatible, LiteLLM | Any new provider |
| `AuthProvider` | Identify the caller | Single admin login | OIDC, multi-user |
| `AgentRunner` | Execute one agent turn | Claude Agent SDK | Other agent runtimes, a fake for tests |
| `ToolRegistry` | Catalog of tool kinds and packs | Built-in registry | New tool kinds |

Rules:

- Core code imports only port types. Adapters are chosen by config (for example `QUEUE_DRIVER=redis`) in one composition root.
- Each port has a contract test suite every adapter must pass.
- Events use our own versioned envelope around SDK messages, so clients don't depend on the SDK's message shape.

## Agent folder layout

Follows Claude Code's own layout, so the SDK loads it with `setting_sources=["project"]` and no custom loader.

```
agents/<slug>/
  agent.json              # platform manifest: name, description, model, credential set, tool packs, approvals, limits
  CLAUDE.md               # system context; @imports memory/MEMORY.md and rules
  .mcp.json               # MCP servers (stdio / http / sse); secrets by reference
  .claude/
    settings.json         # permissions allow/deny, hooks, sandbox, env (no secrets)
    rules/*.md
    skills/<name>/SKILL.md
    agents/<name>.md      # SDK subagents
    commands/*.md
    hooks/                # hook scripts referenced from settings.json
  memory/                 # agent-writable long-term notes
  tools/                  # custom code: scripts or small MCP servers
  workspace/              # task inputs/outputs, scratch
  state/                  # platform-owned: session ids, run index (hidden from the agent)
  .git/                   # per-agent history for diff and rollback
```

| Path | Who writes it | Notes |
| --- | --- | --- |
| `agent.json`, `.claude/settings.json`, `.mcp.json` | UI, Builder | Schema-validated on save; an invalid file blocks runs, not the editor |
| `CLAUDE.md`, `rules/`, `skills/`, `agents/`, `commands/` | UI, Builder | Plain markdown loaded by the SDK |
| `memory/`, `workspace/` | Agent, UI | The only folders the agent can write during a run |
| `tools/`, `hooks/` | UI, Builder | Read-only to the agent at run time |
| `state/` | Platform only | Hidden from the agent's tools |

SDK session transcripts live on the data volume in a per-agent config dir outside the agent folder, so any worker can resume them.

## How a run uses the Claude Agent SDK

1. The worker resolves `agents/<slug>` to a real path; the agent lock is already held (see job runners).
2. It builds `ClaudeAgentOptions`: `cwd` = agent folder, `setting_sources=["project"]`, model from `agent.json` or the credential set, allowed/disallowed tools from the tool packs, `mcp_servers` = `.mcp.json` merged with platform in-process servers, `env` = credential set + referenced secrets only (built from scratch, never a copy of the worker env).
3. It attaches platform guard hooks (`PreToolUse` path/command checks, `PostToolUse` audit) ahead of the agent's own hooks. Agent hooks can add rules, never remove platform ones.
4. `permission_mode="default"` with a `can_use_tool` callback that implements the approvals switch.
5. Partial messages on for token streaming; `resume=<session_id>` for conversation turns.
6. Each SDK message is wrapped in the event envelope and appended to the event log.
7. On finish: session id and run summary to `state/`, folder diff committed to the agent's `.git`, lock released.

Cancel interrupts the SDK client and kills the run's process group. Turn and budget caps come from `agent.json` (`max_turns`, plus a cost/token ceiling checked against streamed `usage`).

## Isolation and sandboxing

| Layer | What it stops | How |
| --- | --- | --- |
| 1. SDK permissions | File tools reaching outside the folder | `PreToolUse` hook realpaths every path; rejects `..`, outside absolute paths, escaping symlinks; denies writes to `state/`, `tools/`, `hooks/` |
| 2. Platform API | File CRUD escaping a folder | One `resolve_agent_path()` used by every call: realpath, prefix check, no-follow; no call takes a raw path |
| 3. OS sandbox | Bash, hooks, custom code, stdio MCP touching anything else | Outer bubblewrap jail around the whole CLI: the agent folder read-only except `memory/` and `workspace/`, nothing else of the data volume, own PID/network namespaces, egress only through the worker's proxy. cgroup v2 memory/CPU/process limits and a wall-clock timeout per run |

- Secrets never live in agent folders; they are injected per run as env vars.
- bubblewrap inside Docker needs user namespaces, so the **worker container only** runs with the extra privileges (custom seccomp/AppArmor profile or `SYS_ADMIN`). API, UI and Envoy stay unprivileged.
- An escape test suite (path traversal, symlinks, `cat ../`, reading other agents) runs in CI and in the devcontainer.
- Network is open by default (HTTP(S) to public addresses through the egress proxy); an agent can opt into a domain allowlist in `agent.json`. See "Phase 3 as built".

## Client API (gRPC)

- Protobuf package `platform.v1` is the single source of truth. Buf lints, generates the Python server stubs and the TypeScript/Go/Rust clients, and blocks breaking changes in CI.
- Python `grpc.aio` server behind Envoy: native gRPC for the CLI, gRPC-Web for the browser, optional JSON/REST transcoding via `google.api.http` annotations.
- Commands are unary (start run, send chat message, cancel, answer approval, file CRUD, credentials). Output is one server stream: `WatchEvents(target, after_offset)`, resumable after any disconnect. No client or bidirectional streams, so browsers can make every call.
- `ReadFile` streams in 1 MiB chunks. `WriteFile` is unary (client streams are banned) and capped at 3 MiB per call; larger uploads will get a chunked-upload flow of unary calls. See decision 18.
- Server reflection + `grpcurl` for debugging, keepalives on long streams, bearer token in metadata from `AuthProvider`.
- gRPC is only the client-facing API; work always goes through the queue to workers.

Draft service outline:

| Service | Methods |
| --- | --- |
| `AgentService` | `ListAgents`, `GetAgent`, `CreateAgent` (from template), `DeleteAgent`, `ValidateAgent`, `ListTemplates` |
| `FileService` | `GetTree`, `ReadFile` (stream), `WriteFile` (unary, etag), `DeleteFile`, `MoveFile`, `CreateDirectory`, `DiffAgent`, `RollbackAgent` |
| `RunService` | `StartRun`, `GetRun`, `ListRuns`, `CancelRun`, `AnswerApproval` |
| `ConversationService` | `CreateConversation`, `SendMessage`, `ListConversations`, `GetConversation`, `SetApprovals` |
| `EventService` | `WatchEvents` (server stream) |
| `CredentialService` | `ListCredentialSets`, `CreateCredentialSet`, `UpdateCredentialSet`, `DeleteCredentialSet`, `TestCredentialSet`, `MakeDefault` |
| `AuthService` | `Login`, `Logout`, `WhoAmI` |

## Background job runners

Workers are a separate Python service (`worker`, N replicas in compose) on asyncio, each running up to `WORKER_CONCURRENCY` jobs. Heavy lifting happens in the Claude CLI subprocess the SDK spawns per run.

1. **Enqueue:** the API writes a `runs` row (`queued`) and calls `JobQueue.enqueue`. The v1 adapter is a thin layer over Redis Streams consumer groups (`XREADGROUP` / `XACK` / `XAUTOCLAIM`), not Celery or arq, so the semantics stay ours.
2. **Claim and lock:** a worker claims the job and takes the agent lock (Redis `SET NX`, 30 s lease renewed by heartbeat). If the agent is busy, the job goes to that agent's FIFO pending list; the worker finishing the current run promotes the next one, so turns stay ordered.
3. **Run:** resolve credentials, build options, run the turn as an asyncio task, append envelopes to the `EventLog` (Redis Stream per run, batched to Postgres). Status: `running` → `awaiting_approval` (when needed) → `succeeded` / `failed` / `cancelled` / `interrupted`.
4. **Control:** workers subscribe to the control channel for active runs. Cancel interrupts the client and kills the process group; an approval answer resolves the future `can_use_tool` waits on; timeout = deny.
5. **Crash recovery:** a reaper marks runs with a dead heartbeat `interrupted` and frees the lock. Sessions live on the data volume, so the next turn resumes on any worker. Agent runs are never retried automatically (side effects); only pre-start failures are.
6. **Shutdown and scale:** SIGTERM stops claiming and gives running jobs a grace period, then marks the rest `interrupted`. Scale by replicas now, autoscale on queue depth later.

Scheduled tasks, if needed later, are a small scheduler process that only enqueues.

## Conversations, subagents and approvals

- Chat-style multi-turn conversations are in v1. One conversation = one SDK session; each message = one queued turn. A one-off task is a one-turn conversation.
- One run per agent at a time; turns and tasks queue behind the agent lock, and the UI shows what they wait on.
- Subagents (`.claude/agents/*.md`) run through the SDK Task tool inside the same run, sandbox and credentials. Events carry the parent tool-use id so the UI nests them.
- Approvals switch: `agent.json` `approvals.mode` = `off` | `risky` | `all` (risky default: Bash, writes outside `workspace/`, MCP tools marked risky), overridable per conversation from the UI. When on, the run pauses with an `approval_requested` event.
- Agent-to-agent is not in v1. The `AgentRunner` port and a reserved `call_agent` slot in the platform MCP registry leave room for it.

## File management

All file access goes through one storage service taking `(agent_slug, relative_path)`, shared by the API and the Builder tools. Tree listing hides `state/` and `.git/` by default; writes use etags to avoid clobbering concurrent edits; JSON and frontmatter files are validated; `agent.json` and `state/*` can't be deleted; agent deletion is a soft delete to trash first. The UI has a file tree, a Monaco editor with schemas for `settings.json`, `.mcp.json` and `agent.json`, and a diff/rollback view backed by the per-agent git history.

## Builder agent

`agents/_builder/` is a normal agent folder whose cross-agent access goes through platform MCP tools (in-process `create_sdk_mcp_server`), not raw file access.

- Context: CLAUDE.md with the folder conventions, skills (`create-agent`, `add-mcp-server`, `write-skill`, `tune-permissions`), agent templates.
- Tools: `list_agents`, `read_agent_file`, `write_agent_file`, `delete_agent_file`, `create_agent(from_template)`, `validate_agent`, `diff_agent`, `test_run_agent(prompt)`, `assign_credential_set(name)`.
- Guardrails: no access to `state/` or secret values; every change is a git commit tagged with the Builder run id; optional approval before it edits permissions or hooks.
- Takes the target agent's lock while writing; can read but not write its own folder.

## Pluggable tools

| Kind | Examples | How it plugs in |
| --- | --- | --- |
| SDK built-ins | WebFetch, WebSearch, Bash, Read/Write/Edit, Task | Allowed tools list; Bash runs in the sandbox |
| External MCP servers | GitHub, Postgres, browser, any third-party server | `.mcp.json` entry; stdio in the sandbox, http/sse open unless allowlisted; secrets by reference |
| Platform MCP tools | Builder tools, `notify_user`, `call_agent` (reserved) | In-process server injected per run |
| Custom code | Scripts in `tools/`, or an MCP server the agent ships | Run via Bash in the sandbox, or registered as stdio MCP for typed inputs |

Tool packs are named bundles (for example `web` = WebFetch + WebSearch) so permissions stay readable.

## Credential manager

Named, encrypted credential sets (endpoint + token). One is the default; each agent picks one in the UI or falls back to the default; a run can override. The worker turns the set into env vars for that run only, so agents use different endpoints concurrently.

| Field | Purpose |
| --- | --- |
| `name`, `description` | Shown in the agent's dropdown |
| `provider` | `anthropic`, `gateway` (Anthropic-compatible base URL), `litellm`, `bedrock`, `vertex` |
| `base_url` | `ANTHROPIC_BASE_URL` for gateway / LiteLLM |
| `auth_type` + secret | API key → `ANTHROPIC_API_KEY`; bearer → `ANTHROPIC_AUTH_TOKEN`; Bedrock → AWS creds + `CLAUDE_CODE_USE_BEDROCK`; Vertex → service account + `CLAUDE_CODE_USE_VERTEX` |
| `headers` | Optional extra headers (`ANTHROPIC_CUSTOM_HEADERS`) |
| `default_model`, `allowed_models` | Model unless `agent.json` overrides; blocks models the endpoint can't serve |
| `is_default` | Exactly one default set |
| `owner` | Empty in v1 (global); allows per-user scoping later |

- Secrets encrypted at rest (AES-GCM, master key from env/KMS); write-only through the API; run records store the set id, never the secret.
- "Test connection" runs a small tool-call smoke test.
- A set in use can't be deleted until its agents move to another set.
- The Builder can assign a set by name but can't read or create secrets.
- The same vault holds MCP and tool secrets, referenced by name.

Providers: Claude via Anthropic API, Bedrock or Vertex, and Anthropic-compatible APIs (for example DeepSeek), call the provider directly. OpenAI, Grok and other non-Claude models go through LiteLLM (optional compose service). Only `litellm` sets touch LiteLLM. The SDK is tuned for Claude, so non-Claude models may be weaker at tool use, long loops, thinking and caching, and may lack server-side tools such as WebSearch.

## Deployment and local development

- v1: Docker Compose on a Linux server. Services: `envoy`, `api`, `worker` (privileged for bubblewrap and cgroups; the worker process itself is unprivileged), `web`, `postgres`, `redis`, optional `litellm`. Agent folders and SDK sessions on a named data volume.
- Local: a devcontainer running the same compose stack (works on Windows via Docker Desktop / WSL2).
- Later: cloud, with the same images; adapters swap through the ports.
- Auth v1: a single admin login behind `AuthProvider`.

## Proposed repository layout

```
agent-playground/
  proto/platform/v1/        # .proto files (source of truth); buf.yaml, buf.gen.yaml
  platform/                 # Python (uv workspace)
    core/                   # domain models, ports, event envelope, config
    adapters/               # postgres, redis, fs store, secrets, sandbox, providers, auth
    api/                    # grpc.aio services + composition root
    worker/                 # job runner, reaper, AgentRunner (Claude Agent SDK)
    builder_tools/          # platform MCP tools for the Builder
    tests/                  # unit + port contract suites + sandbox escape tests
  web/                      # TypeScript UI using generated clients
  cli/                      # future CLI client
  agents/                   # seed content: _builder and templates (runtime agents live on the data volume)
  deploy/                   # compose files, envoy config, seccomp profile
  .devcontainer/
  docs/
```

Python tooling: uv, ruff, pyright (strict), pytest. Web tooling: pnpm, Vite, React, TypeScript (strict), Biome, Vitest; Connect-ES clients over gRPC-Web (decision 16).

## Phase 1 as built

| Area | Where | Notes |
| --- | --- | --- |
| Protobuf API | `proto/platform/v1/{auth,agent,file,common}.proto` | Buf STANDARD lint; `buf breaking` against master in CI |
| Generated code | `platform/proto/src/playground_proto/`, `web/src/gen/` | `scripts/generate.sh`; committed, CI fails on drift |
| Core | `platform/core/src/playground_core/` | Ports (`AgentStore`, `AgentRepository`, `TemplateCatalog`, `SessionStore`, `AuthProvider`, `Clock`), `resolve_agent_path()`, `agent.json` schema, validators, agent/file/auth services. Ruff bans adapter, gRPC, DB and Redis imports here |
| Adapters | `platform/adapters/src/playground_adapters/` | Local-filesystem `AgentStore`, folder `TemplateCatalog`, Postgres agent index (+ migrator), Redis sessions, in-memory variants, static admin `AuthProvider` |
| API | `platform/api/src/playground_api/` | `grpc.aio`, one interceptor for bearer auth + domain-error → status mapping (new RPCs are authenticated by default), health, reflection; `app.py` is the composition root |
| Templates | `agents/templates/{blank,assistant}` | Copied into new agents with name/description stamped in |
| Web UI | `web/` | Login, agent list + create, file tree, Monaco editor with `agent.json` schema, etag conflict handling, diagnostics |
| Deploy | `deploy/`, `.devcontainer/` | Compose: postgres, redis, api, web (nginx), envoy on :8080 (UI and API same origin) |
| Tests | `platform/tests/` | Escape suite, services, port contract suites (memory + Postgres + Redis), end-to-end API |

Phase 1 behaviour worth knowing:

- Validation never blocks a save; `WriteFile` returns diagnostics and errors make `ValidateAgent.runnable` false.
- `state/` and `.git/` are hidden from the tree by default and read-only through the API; `agent.json` can't be deleted or moved.
- Symlinks are refused anywhere in a client path and never listed. The check is done before each file operation, so a race with a process inside the folder is still possible until the Phase 3 sandbox lands.
- Agent deletion moves the folder to `<AGENTS_ROOT>/.trash/` and frees the slug.
- On startup the API indexes agent folders it finds on disk (for seeded system agents such as `_builder`) and drops index rows whose folder is gone.
- `JobQueue`, `EventLog`, `ControlChannel`, `SecretStore`, `Sandbox`, `ModelProvider`, `AgentRunner` and `ToolRegistry` ports arrive with the phases that use them.

## Phase 2 as built

| Area | Where | Notes |
| --- | --- | --- |
| Protobuf API | `proto/platform/v1/{run,event,credential}.proto` | `RunService`, `ConversationService`, `EventService.WatchEvents`, `CredentialService`. `AnswerApproval`/`SetApprovals` come with approvals (Phase 5) |
| Core | `playground_core/{events,providers}.py`, `services/{conversations,credentials,execution}.py` | Event envelope v1, provider env mapping, API-side conversation service, worker-side `RunExecutor` (pure orchestration over ports) |
| Ports | `playground_core/ports.py` | `JobQueue`, `EventLog`, `ControlChannel`, `RunRepository`, `ConversationRepository`, `CredentialRepository`, `SecretStore`, `AgentRunner`; each has a contract suite in `platform/tests/contracts` |
| Adapters | `playground_adapters/{redis_runs,postgres,secrets,memory,wiring}.py` | Redis Streams queue + Lua lease lock, Redis Streams event log, Redis pub/sub control, Postgres repos (migration `0002_runs.sql`), AES-256-GCM secrets. `wiring.py` is the single composition root used by the API and the worker |
| Worker | `platform/worker/src/playground_worker/` | `playground-worker`: N claim slots, heartbeat, reaper, graceful shutdown; `ClaudeAgentRunner` on `ClaudeSDKClient`; `playground-claude-launcher` (env scrubbing); file-tool guard hook |
| Web UI | `web/src/components/{ChatPanel,Credentials}.tsx`, `web/src/lib/{transcript,route}.ts` | Chat tab per agent (conversation list, live streaming transcript, nested subagent tool calls, stop button, queue position), credentials page. Hash routes keep agent/tab/conversation across refresh |
| Deploy | `deploy/worker.Dockerfile`, `deploy/compose.yaml` | `worker` service (unprivileged until Phase 3), shared `agentdata` volume (`/data/agents`, `/data/sessions`), `SECRETS_KEY` required by API and worker |

How a turn flows:

1. `SendMessage` (or `StartRun`, a one-turn conversation) validates the agent and resolves a credential set, writes a `queued` run, appends `user_message` + `run_status` events and enqueues a `Job(run_id, agent_slug)`.
2. **Queue** (Redis): each agent has a FIFO list; a `wake` stream (consumer group `workers`) carries "look at agent X" hints. A worker that reads a wake tries the agent lock (`SET NX PX`, token compared in Lua on renew/release) and, holding it, takes the agent's in-flight job or pops the next one. The holder releases the lock *before* re-waking the agent if more jobs wait, so no wake is lost. A lock that expires with a job in flight (dead worker) gives that job back exactly once as `recovered`, and the executor marks it `interrupted` instead of re-running it (decision 4: no automatic retries). Every worker's reaper also re-wakes orphaned agents and interrupts `running` runs with a stale DB heartbeat.
3. **Executor**: subscribes to the control channel, compare-and-sets `queued → running`, resolves credentials (run override → conversation override → `agent.json` `credential_set` name → default), decrypts the secret, builds the provider env and model, then calls the `AgentRunner`. Heartbeats renew the lease and the DB row; a lost lease cancels the run as `interrupted`.
4. **Runner**: `ClaudeSDKClient` with `cwd` = agent folder, `setting_sources=["project"]`, `claude_code` system prompt preset, partial messages on, `resume=<session_id>` for later turns, `max_turns`/`max_budget_usd` from `agent.json`, `can_use_tool` allowing everything (the approvals switch plugs in here), and the platform `PreToolUse` guard. SDK messages become platform events (`Translator`); session id is saved on the conversation.
5. **Events**: one Redis stream per conversation with our own offsets (entry id `0-<offset>`). `WatchEvents(conversation_id, after_offset)` replays then follows, with heartbeats (offset 0) every 15 s while idle. The UI folds events into a transcript, replaying from 0 on load and resuming from the last offset after a disconnect.
6. **Cancel**: a queued run is cancelled in the DB directly (CAS); a running one gets a control message, the runner interrupts the SDK client and returns `cancelled`. The SDK session is kept, so the next turn resumes it.

Isolation in Phase 2 (honest status; Phase 3 closes the rest):

- **Env**: the SDK starts the CLI with the worker's whole environment plus `options.env`. `cli_path` points at `playground-claude-launcher`, which drops every variable the worker itself had (names passed in `PLAYGROUND_DROP_ENV`) unless the run set it. A run's env is built from scratch: `PATH`, `HOME`/`CLAUDE_CONFIG_DIR`/`TMPDIR` under `/data/sessions/<slug>`, non-secret proxy/CA passthrough, and the provider env. Tested with the real CLI: a Bash `env` dump inside a run contains no worker variables.
- **File tools**: the platform `PreToolUse` hook realpaths every Read/Glob/Grep/LS/Write/Edit/MultiEdit/NotebookEdit path; anything outside the agent folder, or inside `state/`/`.git/`, is denied, and writes are limited to `memory/` and `workspace/`. Agent hooks run too; a deny from any hook wins.
- **Not yet contained** (closed in Phase 3): Bash commands, the agent's own hook scripts and stdio MCP servers were ordinary processes of the worker user, and children started by Bash could outlive a cancel.

Known gaps carried forward: events live only in Redis (AOF on), not archived to Postgres; Bedrock/Vertex credential sets are rejected until Phase 5; `TestCredentialSet` is not implemented; deleting an agent leaves its conversations in the database.

## Phase 3 as built

| Area | Where | Notes |
| --- | --- | --- |
| Jail | `playground_worker/jail.py`, `launcher.py` | `JailSpec` (built by the runner, passed in `PLAYGROUND_JAIL`, scrubbed before exec) → bwrap argv. The launcher joins the run's cgroup, sets rlimits and execs `bwrap … -- claude` |
| Egress | `playground_worker/egress.py`, `jail_init.py` | Per-run HTTP/CONNECT proxy on a unix socket in `RUN_RUNTIME_DIR`; `jail_init` (stdlib-only, first process in the jail) relays `127.0.0.1:3128` to it. Blocked requests get a 403 and a `notice` event |
| Limits | `playground_worker/cgroups.py`, `deploy/worker-entrypoint.sh` | `run-<id>` cgroups under the delegated `RUN_CGROUP_ROOT` (`memory.max`, `memory.swap.max`, `pids.max`, `cpu.max`); `cgroup.kill` on exit; OOM kills become a `notice` |
| Manifest | `playground_core/manifest.py` | `limits.timeout_seconds`, `memory_mb`, `cpus`, `max_processes`; `network.allowlist` is now enforced. They reach the runner as `RunRequest.limits` / `network_allowlist` |
| Guard | `playground_worker/guard.py` | Unchanged for normal agents; the Builder may also read other agents' folders |
| Ops | `playground-worker-selfcheck` | Checks bwrap and (with `--require-cgroups`) that a jailed process lands in a limited cgroup. CI runs it in the worker image |
| Tests | `platform/tests/test_sandbox.py`, `test_sandbox_units.py` | Escape suite with the real CLI: reading other agents and worker files, writing outside `memory/`/`workspace/`, `state/` visibility, worker `/proc` and env, direct network, proxy to private addresses, agent hook scripts, Builder view, background processes after the run, timeout, symlinked `workspace/` |

What a jailed run sees:

| Path | Access |
| --- | --- |
| `/usr`, `/bin`, `/lib*`, selected `/etc` files (TLS, users, resolver, linker) | read-only |
| The bundled CLI's directory; the worker's Python stdlib (for `jail_init`); CA files named by passthrough env | read-only |
| `agents/<slug>/` | read-only; `memory/` and `workspace/` writable; `state/` and `.git/` replaced by empty read-only tmpfs |
| `/data/sessions/<slug>/` (HOME, `CLAUDE_CONFIG_DIR`, TMPDIR) | writable |
| `/tmp`, `/run`, `/dev`, `/proc` | fresh per run; `/proc` shows only the run's processes |
| Everything else (`/data/agents/*`, `/opt/venv`, `/home`, worker files) | absent |
| Builder (`_builder`) | additionally every agent folder read-only, without `state/`, `.git/` and `.trash/` |

Namespaces: user (where needed), PID, IPC, UTS, cgroup and network; capabilities dropped; `--new-session` (no terminal injection); `--die-with-parent`, so killing the CLI or the worker ends the whole tree, and the PID namespace takes background processes with it.

Network: the jail has only loopback. `HTTP(S)_PROXY` point at `jail_init`'s relay and `NO_PROXY` is empty, so the CLI, WebFetch, git, pip, npm and curl work through the proxy. The proxy always lets the model endpoint through (the credential set's `ANTHROPIC_BASE_URL`, else `api.anthropic.com`). With `network.allowlist` only matching hosts (`example.com`, `*.example.com`) are reachable; without one any public address is, and loopback, RFC 1918, CGNAT, link-local and other reserved addresses are refused (the resolved address is checked and then connected to, so DNS rebinding can't swap it). Behind an upstream proxy (`HTTPS_PROXY` in the worker env, `NO_PROXY` honoured) names the worker can't resolve are passed on, and that proxy becomes the egress control. Raw TCP, SSH and UDP don't work from a jail.

Limits: per run, `agent.json` `limits.*` or the worker's `RUN_TIMEOUT_SECONDS` (3600), `RUN_MEMORY_MB` (4096), `RUN_CPUS` (2), `RUN_MAX_PROCESSES` (512). The timeout fails the run with "time limit". cgroup limits need cgroup v2 and the entrypoint's delegation (privileged container, `cgroup: private`); elsewhere only the timeout and rlimits apply, and the worker logs why at start.

Settings: `SANDBOX_MODE` (`bwrap` | `none`, development only), `SANDBOX_NETWORK` (`proxy` | `host`; `host` shares the worker's network and can reach Redis/Postgres), `RUN_CGROUP_ROOT` (set by the entrypoint), `RUN_RUNTIME_DIR`, `BUILDER_SLUG`.

Still open after Phase 3: the agent can read its own `tools/`, `.claude/` and `.mcp.json` (by design) and anything world-readable under `/usr`; the worker container itself is privileged, so a kernel escape from a jail is a container escape (a VM or gVisor runtime is the next step if agents become untrusted multi-tenant code); per-run disk usage is bounded only by the file-size rlimit, not a quota.

## Phased delivery

1. **Foundations:** repo layout, devcontainer, compose, core ports with Postgres and Redis adapters, simple admin login, protobuf API with Buf, grpc.aio server behind Envoy, storage service with path resolution, agent templates, file CRUD, UI tree + editor. Done when you can create and edit an agent folder from the browser.
2. **Runs:** queue, worker running the SDK, credential manager with a default set, event log, `WatchEvents`, chat-style multi-turn conversations, subagent nesting, cancel. Done when a chat streams live, resumes on the next turn, and survives a page refresh.
3. **Isolation:** guard hooks, bubblewrap sandbox in the worker container, resource limits, optional allowlist, secrets injection. Done when the escape suite passes.
4. **Builder:** `_builder` agent, platform MCP tools, validators, per-agent git history and diff/rollback. Done when the Builder creates a working agent from a one-line description.
5. **Tools and providers:** tool registry and packs, MCP server management UI, approvals switch, LiteLLM provider path for non-Claude models.
6. **Hardening:** quotas and cost caps, audit log, observability. Multi-user auth, cloud deployment and agent-to-agent come later through the existing ports.
