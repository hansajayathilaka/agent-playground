# Agent Playground: architecture

Status: final plan, 2026-11-07. Implementation starts with Phase 1.
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
| `Sandbox` | Wrap a run's processes | SDK bubblewrap sandbox | Container per run, Firecracker |
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
| 3. OS sandbox | Bash, custom code, stdio MCP touching anything else | SDK bubblewrap sandbox: writes limited to the agent folder, reads outside denied, children inherit it. CPU, memory and timeouts from the worker |

- Secrets never live in agent folders; they are injected per run as env vars.
- bubblewrap inside Docker needs user namespaces, so the **worker container only** runs with the extra privileges (custom seccomp/AppArmor profile or `SYS_ADMIN`). API, UI and Envoy stay unprivileged.
- An escape test suite (path traversal, symlinks, `cat ../`, reading other agents) runs in CI and in the devcontainer.
- Network is open by default; an agent can opt into a domain allowlist in `agent.json`.

## Client API (gRPC)

- Protobuf package `platform.v1` is the single source of truth. Buf lints, generates the Python server stubs and the TypeScript/Go/Rust clients, and blocks breaking changes in CI.
- Python `grpc.aio` server behind Envoy: native gRPC for the CLI, gRPC-Web for the browser, optional JSON/REST transcoding via `google.api.http` annotations.
- Commands are unary (start run, send chat message, cancel, answer approval, file CRUD, credentials). Output is one server stream: `WatchEvents(target, after_offset)`, resumable after any disconnect. No client or bidirectional streams, so browsers can make every call.
- Files over 1 MB are streamed in chunks (`ReadFile` / `WriteFile`), staying under gRPC's 4 MB default.
- Server reflection + `grpcurl` for debugging, keepalives on long streams, bearer token in metadata from `AuthProvider`.
- gRPC is only the client-facing API; work always goes through the queue to workers.

Draft service outline:

| Service | Methods |
| --- | --- |
| `AgentService` | `ListAgents`, `GetAgent`, `CreateAgent` (from template), `DeleteAgent`, `ValidateAgent` |
| `FileService` | `GetTree`, `ReadFile` (stream), `WriteFile` (stream, etag), `DeleteFile`, `MoveFile`, `DiffAgent`, `RollbackAgent` |
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

- v1: Docker Compose on a Linux server. Services: `envoy`, `api`, `worker` (privileged for bubblewrap), `web`, `postgres`, `redis`, optional `litellm`. Agent folders and SDK sessions on a named data volume.
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

Python tooling: uv, ruff, pyright (strict), pytest. Web tooling decided in Phase 1.

## Phased delivery

1. **Foundations:** repo layout, devcontainer, compose, core ports with Postgres and Redis adapters, simple admin login, protobuf API with Buf, grpc.aio server behind Envoy, storage service with path resolution, agent templates, file CRUD, UI tree + editor. Done when you can create and edit an agent folder from the browser.
2. **Runs:** queue, worker running the SDK, credential manager with a default set, event log, `WatchEvents`, chat-style multi-turn conversations, subagent nesting, cancel. Done when a chat streams live, resumes on the next turn, and survives a page refresh.
3. **Isolation:** guard hooks, bubblewrap sandbox in the worker container, resource limits, optional allowlist, secrets injection. Done when the escape suite passes.
4. **Builder:** `_builder` agent, platform MCP tools, validators, per-agent git history and diff/rollback. Done when the Builder creates a working agent from a one-line description.
5. **Tools and providers:** tool registry and packs, MCP server management UI, approvals switch, LiteLLM provider path for non-Claude models.
6. **Hardening:** quotas and cost caps, audit log, observability. Multi-user auth, cloud deployment and agent-to-agent come later through the existing ports.
