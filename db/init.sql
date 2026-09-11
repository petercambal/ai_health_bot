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
    user_id BIGINT NOT NULL REFERENCES health_tracker.auth_user (id) ON DELETE CASCADE,
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

-- One row per Telegram user who has linked a Garmin account. session_json is the
-- garminconnect client's serialized token (dumps()/loads()) - never the password,
-- which is only ever held in memory during the one-time interactive bootstrap login
-- (see app/garmin/bootstrap.py). This is what lets sync run unattended (cron, or a
-- Telegram-triggered request) without ever storing a Garmin password anywhere.
CREATE TABLE IF NOT EXISTS health_tracker.garmin_account (
    user_id BIGINT PRIMARY KEY REFERENCES health_tracker.auth_user (telegram_id) ON DELETE CASCADE,
    garmin_email TEXT NOT NULL,
    session_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
