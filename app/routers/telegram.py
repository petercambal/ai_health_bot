import logging

from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings
from app.telegram.bot import bot, dp

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(settings.telegram_webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret token")

    payload = await request.json()
    update = Update.model_validate(payload)

    try:
        await dp.feed_webhook_update(bot, update)
    except Exception:
        logger.exception("Failed to process Telegram update %s", update.update_id)

    return {"ok": True}
