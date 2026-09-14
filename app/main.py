import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import database
from app.config import settings
from app.routers import dashboard, garmin_login, landing, nutrition_login, telegram
from app.scheduler import start_scheduler, stop_scheduler
from app.telegram.bot import setup_commands, setup_webhook, teardown_webhook

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    await setup_webhook()
    await setup_commands()
    start_scheduler()
    logger.info("Application startup complete")
    try:
        yield
    finally:
        stop_scheduler()
        await teardown_webhook()
        await database.disconnect()
        logger.info("Application shutdown complete")


app = FastAPI(title="Telegram Health Tracker", lifespan=lifespan)
app.include_router(telegram.router)
app.include_router(dashboard.router)
app.include_router(garmin_login.router)
app.include_router(nutrition_login.router)
app.include_router(landing.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
