CREATE TABLE agents (
    slug        text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    template    text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL
);
