import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message

from app import database
from app.config import settings
from app.llm.service import handle_user_message

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()
router = Router()
dp.include_router(router)


@router.message()
async def on_message(message: Message) -> None:
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    if not await database.is_authorized_user(telegram_id):
        logger.warning("Rejected message from unauthorized telegram_id=%s", telegram_id)
        await message.answer("Nemáš prístup k tomuto botovi.")
        return

    if not message.text:
        await message.answer("Zatiaľ viem spracovať iba textové správy.")
        return

    reply = await handle_user_message(user_id=telegram_id, text=message.text)
    await message.answer(reply)


async def setup_webhook() -> None:
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )
    logger.info("Telegram webhook set to %s", settings.telegram_webhook_url)


async def teardown_webhook() -> None:
    await bot.delete_webhook()
    await bot.session.close()
