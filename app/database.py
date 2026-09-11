import json
import logging
from datetime import UTC, datetime, timedelta

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


async def get_distinct_types(user_id: int) -> list[str]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT typ FROM health_records WHERE user_id = $1 ORDER BY typ",
        user_id,
    )
    return [r["typ"] for r in rows]
