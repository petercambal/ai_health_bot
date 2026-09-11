import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message

from app import database
from app.config import settings
from app.garmin.link_token import generate_link_token
from app.llm.service import handle_user_message

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()
router = Router()
dp.include_router(router)

_COMMANDS = [
    BotCommand(command="help", description="Ako bota používať"),
    BotCommand(command="garmin_link", description="Prepojiť Garmin účet"),
    BotCommand(command="system_prompt", description="Zobraziť/nastaviť/zmazať systémový prompt"),
]


async def _check_authorized(message: Message) -> int | None:
    if not message.from_user:
        return None

    telegram_id = message.from_user.id
    if not await database.is_authorized_user(telegram_id):
        logger.warning("Rejected message from unauthorized telegram_id=%s", telegram_id)
        await message.answer("Nemáš prístup k tomuto botovi.")
        return None

    return telegram_id


@router.message(Command("garmin_link"))
async def on_garmin_link(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    link_token = generate_link_token(telegram_id)
    url = f"{settings.telegram_webhook_base_url.rstrip('/')}/garmin-login?token={link_token}"
    await message.answer(f"Prepoj svoj Garmin účet tu (odkaz platí 30 minút):\n{url}")


@router.message(Command("system_prompt"))
async def on_system_prompt(message: Message, command: CommandObject) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    text = (command.args or "").strip()

    if not text:
        current = await database.get_system_prompt(telegram_id)
        if current:
            await message.answer(f"Aktuálny systémový prompt:\n\n{current}")
        else:
            await message.answer(
                "Systémový prompt nie je nastavený. Nastav ho takto:\n"
                "/system_prompt <text>\n\n"
                "Napr: /system_prompt Si môj osobný kondičný tréner, mám 196 cm...\n\n"
                "Zmaž ho príkazom: /system_prompt clear"
            )
        return

    if text.lower() in {"clear", "reset", "vymazať", "vymazat"}:
        await database.set_system_prompt(telegram_id, None)
        await message.answer("Systémový prompt zmazaný.")
        return

    await database.set_system_prompt(telegram_id, text)
    await message.answer("Systémový prompt uložený ✅ (platí od ďalšej správy)")


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    await message.answer(
        "Môžeš mi jednoducho napísať čokoľvek, napr.:\n"
        "- 'Dnes som sa vážil, 88kg' - uložím to\n"
        "- 'Aká bola moja váha za posledný mesiac?' - pozriem históriu\n"
        "- 'Stiahni Garmin dáta za včera' - zosynchronizujem Garmin\n\n"
        "Príkazy:\n"
        "/garmin_link - prepojiť Garmin účet\n"
        "/system_prompt <text> - nastaviť tvoju personu pre asistenta "
        "(bez textu zobrazí aktuálnu, 'clear' ju zmaže)"
    )


@router.message()
async def on_message(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
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


async def setup_commands() -> None:
    await bot.set_my_commands(_COMMANDS)
    logger.info("Telegram bot commands registered")


async def teardown_webhook() -> None:
    await bot.delete_webhook()
    await bot.session.close()
