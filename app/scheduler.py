import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def sync_garmin_data() -> None:
    """Placeholder job demonstrating the scheduled-task wiring; replace with a real
    Garmin Connect sync once credentials/API access are available."""
    logger.info("sync_garmin_data: running scheduled Garmin sync (dummy)")


def start_scheduler() -> None:
    scheduler.add_job(
        sync_garmin_data,
        trigger=CronTrigger(hour=3, minute=0),
        id="sync_garmin_data",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
