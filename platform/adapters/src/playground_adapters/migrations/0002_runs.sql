CREATE TABLE credential_sets (
    id             text PRIMARY KEY,
    name           text NOT NULL UNIQUE,
    description    text NOT NULL DEFAULT '',
    provider       text NOT NULL,
    base_url       text NOT NULL DEFAULT '',
    auth_type      text NOT NULL,
    headers        jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_model  text NOT NULL DEFAULT '',
    allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb,
    is_default     boolean NOT NULL DEFAULT false,
    secret         bytea NOT NULL DEFAULT ''::bytea,
    owner          text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL
);
-- At most one default set.
CREATE UNIQUE INDEX credential_sets_one_default ON credential_sets ((true)) WHERE is_default;

CREATE TABLE conversations (
    id                text PRIMARY KEY,
    agent_slug        text NOT NULL,
    title             text NOT NULL DEFAULT '',
    session_id        text NOT NULL DEFAULT '',
    credential_set_id text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL
);
CREATE INDEX conversations_by_agent ON conversations (agent_slug, updated_at DESC);

CREATE TABLE runs (
    id                text PRIMARY KEY,
    conversation_id   text NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    agent_slug        text NOT NULL,
    prompt            text NOT NULL,
    status            text NOT NULL,
    created_at        timestamptz NOT NULL,
    started_at        timestamptz,
    finished_at       timestamptz,
    heartbeat_at      timestamptz,
    worker            text NOT NULL DEFAULT '',
    error             text NOT NULL DEFAULT '',
    credential_set_id text NOT NULL DEFAULT '',
    input_tokens      bigint NOT NULL DEFAULT 0,
    output_tokens     bigint NOT NULL DEFAULT 0,
    cost_usd          double precision NOT NULL DEFAULT 0,
    num_turns         integer NOT NULL DEFAULT 0,
    duration_ms       bigint NOT NULL DEFAULT 0,
    seq               bigserial
);
CREATE INDEX runs_by_conversation ON runs (conversation_id, seq);
CREATE INDEX runs_by_agent ON runs (agent_slug, seq DESC);
CREATE INDEX runs_running ON runs (heartbeat_at) WHERE status = 'running';
