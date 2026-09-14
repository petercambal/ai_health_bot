import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import database
from app.garmin import sync as garmin_sync
from app.integrations import ensure_day_synced
from app.llm.service import handle_user_message
from app.nutrition import sync as nutrition_sync
from app.telegram.bot import DAILY_REPORT_PROMPT, send_message

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def sync_garmin_data() -> None:
    """Catches up every user who has linked a Garmin account on any days missed
    since their last sync, up to and including yesterday."""
    user_ids = await database.list_garmin_accounts()
    for user_id in user_ids:
        try:
            synced_days = await garmin_sync.sync_catch_up(user_id)
            logger.info("Garmin sync complete for user_id=%s: %s", user_id, synced_days)
        except Exception:
            logger.exception("Garmin sync failed for user_id=%s", user_id)


async def sync_nutrition_data() -> None:
    """Catches up every user who has linked a kaloricketabulky.sk account on any
    days missed since their last sync, up to and including yesterday - same shape as
    sync_garmin_data, run alongside it so the daily digest has both sides covered."""
    user_ids = await database.list_nutrition_accounts()
    for user_id in user_ids:
        try:
            synced_days = await nutrition_sync.sync_catch_up(user_id)
            logger.info("Nutrition sync complete for user_id=%s: %s", user_id, synced_days)
        except Exception:
            logger.exception("Nutrition sync failed for user_id=%s", user_id)


async def send_daily_reports() -> None:
    """Proactively pushes the /daily_report content to every active user each
    morning. force=True on the sync so the report reflects same-day corrections
    (e.g. a food diary entry edited after the regular catch-up sync already ran),
    not just whatever was already cached - unlike a manually-triggered /daily_report,
    which uses force=False since the user is asking right now, not hours later."""
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    user_ids = await database.list_active_users()
    for user_id in user_ids:
        try:
            await ensure_day_synced(user_id, yesterday, force=True)
            reply = await handle_user_message(user_id=user_id, text=DAILY_REPORT_PROMPT)
            await send_message(user_id, reply)
            logger.info("Daily report sent for user_id=%s", user_id)
        except Exception:
            logger.exception("Daily report push failed for user_id=%s", user_id)


def start_scheduler() -> None:
    scheduler.add_job(
        sync_garmin_data,
        trigger=CronTrigger(hour=9, minute=0),
        id="sync_garmin_data",
        replace_existing=True,
    )
    scheduler.add_job(
        sync_nutrition_data,
        trigger=CronTrigger(hour=9, minute=0),
        id="sync_nutrition_data",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_reports,
        trigger=CronTrigger(hour=9, minute=0),
        id="send_daily_reports",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
