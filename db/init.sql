CREATE SCHEMA IF NOT EXISTS health_tracker;

CREATE TABLE IF NOT EXISTS health_tracker.auth_user (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    display_name TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user persona/profile text for Gemini, settable via the /system_prompt
-- Telegram command (see app/telegram/bot.py) instead of a deploy-time file.
ALTER TABLE health_tracker.auth_user ADD COLUMN IF NOT EXISTS system_prompt TEXT;

CREATE TABLE IF NOT EXISTS health_tracker.health_records (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    typ VARCHAR(50) NOT NULL,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_user_typ_ts
    ON health_tracker.health_records (user_id, typ, ts DESC);

-- Adds the FK for tables created before it existed (CREATE TABLE IF NOT EXISTS above
-- is a no-op on an already-existing table, so ADD CONSTRAINT ... REFERENCES has to be
-- applied separately). Postgres has no ADD CONSTRAINT IF NOT EXISTS, hence the DO block.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'health_tracker.health_records'::regclass
          AND confrelid = 'health_tracker.auth_user'::regclass
    ) THEN
        ALTER TABLE health_tracker.health_records
            ADD CONSTRAINT health_records_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE;
    END IF;
END $$;

-- Superseded by health_tracker.integrations below (one generic table for every
-- linked external service, not one table per service) - kept here only so an
-- existing deployment's table isn't dropped out from under it. No app code
-- reads/writes this anymore; see the migration note above the integrations table.
CREATE TABLE IF NOT EXISTS health_tracker.garmin_account (
    user_id BIGINT PRIMARY KEY REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE,
    garmin_email TEXT NOT NULL,
    session_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per (user, linked external service) - Garmin, kaloricketabulky.sk, and
-- whatever comes next all share this table rather than getting their own
-- account/session table each, since the app is meant to grow more sources over time
-- and a scheduler that wants "every linked account across every service" would
-- otherwise have to UNION a growing list of tables. `credentials` holds whatever a
-- given service's sync code needs to authenticate (a Garmin session token, a cookie
-- jar for kaloricketabulky.sk, etc.) - never a plaintext password. `label` is just
-- for human-readable display (e.g. the linked account's email).
CREATE TABLE IF NOT EXISTS health_tracker.integrations (
    user_id BIGINT NOT NULL REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    label TEXT,
    credentials JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, service)
);

-- One-time backfill from the old garmin_account table (safe to re-run - ON CONFLICT
-- DO NOTHING skips rows already migrated).
INSERT INTO health_tracker.integrations (user_id, service, label, credentials, updated_at)
SELECT
    user_id,
    'garmin',
    garmin_email,
    jsonb_build_object('email', garmin_email, 'session_json', session_json),
    updated_at
FROM health_tracker.garmin_account
ON CONFLICT (user_id, service) DO NOTHING;

-- One row per Gemini API call (a single Telegram message can trigger two - the
-- initial call, and a follow-up when get_health_records feeds data back to the
-- model), for cost/usage auditing.
CREATE TABLE IF NOT EXISTS health_tracker.token_usage (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    response_tokens INTEGER,
    total_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_token_usage_user_ts
    ON health_tracker.token_usage (user_id, ts DESC);
