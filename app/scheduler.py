import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import database
from app.garmin import sync as garmin_sync

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def sync_garmin_data() -> None:
    """Catches up every user who has linked a Garmin account (garmin_account row)
    on any days missed since their last sync, up to and including yesterday."""
    user_ids = await database.list_garmin_accounts()
    for user_id in user_ids:
        try:
            synced_days = await garmin_sync.sync_catch_up(user_id)
            logger.info("Garmin sync complete for user_id=%s: %s", user_id, synced_days)
        except Exception:
            logger.exception("Garmin sync failed for user_id=%s", user_id)


def start_scheduler() -> None:
    scheduler.add_job(
        sync_garmin_data,
        trigger=CronTrigger(hour=9, minute=0),
        id="sync_garmin_data",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
