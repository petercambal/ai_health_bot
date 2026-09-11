CREATE SCHEMA IF NOT EXISTS health_tracker;

CREATE TABLE IF NOT EXISTS health_tracker.auth_user (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    display_name TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS health_tracker.health_records (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    typ VARCHAR(50) NOT NULL,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_user_typ_ts
    ON health_tracker.health_records (user_id, typ, ts DESC);
