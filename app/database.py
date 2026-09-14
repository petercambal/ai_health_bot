import json
import logging
from datetime import UTC, date, datetime, timedelta

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# Tables live in their own schema so this app can share a Postgres instance/DB with
# other projects without name clashes. Must match the schema created in db/init.sql.
SCHEMA = "health_tracker"


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=10,
        init=_init_connection,
        # Set as a startup parameter (not a plain SET) so it survives the RESET ALL
        # asyncpg runs on every connection release back to the pool.
        server_settings={"search_path": f"{SCHEMA},public"},
    )
    logger.info("Database pool created")


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized - call connect() first")
    return _pool


async def is_authorized_user(telegram_id: int) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM auth_user WHERE telegram_id = $1 AND is_active = TRUE",
        telegram_id,
    )
    return row is not None


async def add_auth_user(telegram_id: int, display_name: str | None = None) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO auth_user (telegram_id, display_name)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name
        """,
        telegram_id,
        display_name,
    )


async def get_system_prompt(user_id: int) -> str | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT system_prompt FROM auth_user WHERE telegram_id = $1",
        user_id,
    )
    return row["system_prompt"] if row else None


async def set_system_prompt(user_id: int, text: str | None) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE auth_user SET system_prompt = $1 WHERE telegram_id = $2",
        text,
        user_id,
    )


async def insert_health_record(
    user_id: int, typ: str, data: dict, ts: datetime | None = None
) -> int:
    pool = get_pool()
    ts = ts or datetime.now(UTC)
    row = await pool.fetchrow(
        """
        INSERT INTO health_records (user_id, ts, typ, data)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        user_id,
        ts,
        typ,
        data,
    )
    return row["id"]


async def get_health_records(user_id: int, typ: str, days_back: int) -> list[dict]:
    pool = get_pool()
    since = datetime.now(UTC) - timedelta(days=days_back)
    rows = await pool.fetch(
        """
        SELECT id, ts, typ, data
        FROM health_records
        WHERE user_id = $1 AND typ = $2 AND ts >= $3
        ORDER BY ts DESC
        """,
        user_id,
        typ,
        since,
    )
    return [
        {"id": r["id"], "ts": r["ts"].isoformat(), "typ": r["typ"], "data": r["data"]}
        for r in rows
    ]


async def get_records_for_dashboard(user_id: int, typ: str, limit: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, ts, typ, data
        FROM health_records
        WHERE user_id = $1 AND typ = $2
        ORDER BY ts DESC
        LIMIT $3
        """,
        user_id,
        typ,
        limit,
    )
    return [
        {"id": r["id"], "ts": r["ts"].isoformat(), "typ": r["typ"], "data": r["data"]}
        for r in reversed(rows)
    ]


async def get_last_workout(user_id: int, variant: str) -> dict | None:
    """Most recently logged workout session for this variant - health_records rows
    with typ='workout' store one row per session (see log_workout_session), so this
    is just the latest one matching data->>'variant'."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT ts, data
        FROM health_records
        WHERE user_id = $1 AND typ = 'workout' AND data->>'variant' = $2
        ORDER BY ts DESC
        LIMIT 1
        """,
        user_id,
        variant,
    )
    if row is None:
        return None
    return {"ts": row["ts"].isoformat(), **row["data"]}


async def get_distinct_types(user_id: int) -> list[str]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT typ FROM health_records WHERE user_id = $1 ORDER BY typ",
        user_id,
    )
    return [r["typ"] for r in rows]


async def replace_health_record_for_day(user_id: int, typ: str, day: date, data: dict) -> None:
    """Deletes any existing record(s) for this user/typ/day and inserts a fresh one.

    Used for sources like Garmin where a day is a single evolving object, not an
    append-only log - re-syncing a day should replace it, not accumulate duplicates.
    """
    pool = get_pool()
    ts = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM health_records WHERE user_id = $1 AND typ = $2 AND ts::date = $3",
            user_id,
            typ,
            day,
        )
        await conn.execute(
            "INSERT INTO health_records (user_id, ts, typ, data) VALUES ($1, $2, $3, $4)",
            user_id,
            ts,
            typ,
            data,
        )


async def get_last_synced_date(user_id: int, typ: str) -> date | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT MAX(ts::date) AS d FROM health_records WHERE user_id = $1 AND typ = $2",
        user_id,
        typ,
    )
    return row["d"] if row else None


async def get_integration_credentials(user_id: int, service: str) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT credentials FROM integrations WHERE user_id = $1 AND service = $2",
        user_id,
        service,
    )
    return row["credentials"] if row else None


async def save_integration_credentials(
    user_id: int, service: str, credentials: dict, label: str | None = None
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO integrations (user_id, service, label, credentials, updated_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (user_id, service) DO UPDATE SET
            label = EXCLUDED.label,
            credentials = EXCLUDED.credentials,
            updated_at = NOW()
        """,
        user_id,
        service,
        label,
        credentials,
    )


async def list_integration_users(service: str) -> list[int]:
    pool = get_pool()
    rows = await pool.fetch("SELECT user_id FROM integrations WHERE service = $1", service)
    return [r["user_id"] for r in rows]


_GARMIN_SERVICE = "garmin"
_NUTRITION_SERVICE = "nutrition"


async def get_garmin_session(user_id: int) -> str | None:
    creds = await get_integration_credentials(user_id, _GARMIN_SERVICE)
    return creds["session_json"] if creds else None


async def get_garmin_email(user_id: int) -> str | None:
    creds = await get_integration_credentials(user_id, _GARMIN_SERVICE)
    return creds["email"] if creds else None


async def save_garmin_session(user_id: int, garmin_email: str, session_json: str) -> None:
    await save_integration_credentials(
        user_id,
        _GARMIN_SERVICE,
        {"email": garmin_email, "session_json": session_json},
        label=garmin_email,
    )


async def list_garmin_accounts() -> list[int]:
    return await list_integration_users(_GARMIN_SERVICE)


async def get_nutrition_cookies(user_id: int) -> dict | None:
    creds = await get_integration_credentials(user_id, _NUTRITION_SERVICE)
    return creds["cookies"] if creds else None


async def save_nutrition_cookies(user_id: int, cookies: dict, label: str | None = None) -> None:
    await save_integration_credentials(user_id, _NUTRITION_SERVICE, {"cookies": cookies}, label=label)


async def list_nutrition_accounts() -> list[int]:
    return await list_integration_users(_NUTRITION_SERVICE)


async def log_token_usage(
    user_id: int,
    model: str,
    prompt_tokens: int | None,
    response_tokens: int | None,
    total_tokens: int | None,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO token_usage (user_id, model, prompt_tokens, response_tokens, total_tokens)
        VALUES ($1, $2, $3, $4, $5)
        """,
        user_id,
        model,
        prompt_tokens,
        response_tokens,
        total_tokens,
    )


async def get_token_usage_summary(user_id: int, year: int, month: int) -> list[dict]:
    """Per-model token usage totals for one calendar month - a per-model breakdown
    rather than one grand total, since cost is priced differently per model and the
    user may have switched models (GEMINI_MODEL) partway through the month."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT
            model,
            COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens,
            COALESCE(SUM(response_tokens), 0)::bigint AS response_tokens,
            COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
            COUNT(*) AS call_count
        FROM token_usage
        WHERE user_id = $1
          AND ts >= make_date($2, $3, 1)
          AND ts < make_date($2, $3, 1) + INTERVAL '1 month'
        GROUP BY model
        ORDER BY model
        """,
        user_id,
        year,
        month,
    )
    return [dict(r) for r in rows]
