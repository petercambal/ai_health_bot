import logging
import re
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message

from app import database
from app.config import settings
from app.garmin.link_token import generate_link_token
from app.llm import pricing as token_pricing
from app.llm.service import handle_user_message
from app.telegram.formatting import to_telegram_html

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

_MONTH_ARG_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# The bot always replies in the language the user wrote in (see
# app/llm/service.py's routing instruction), so this Slovak prompt just makes /daily_report
# a shortcut for typing it out - no need for it to match the UI's own (English) language.
_DAILY_REPORT_PROMPT = (
    "Dobré ráno! Zhrň mi včerajší deň na základe všetkých mojich zaznamenaných dát "
    "(nielen Garmin - aj váha, strava, prípadne glukomer, čokoľvek mám zapísané). Ak "
    "tam bola nejaká výnimočná hodnota (peak alebo naopak zlá/nízka hodnota), vypichni "
    "ju a vysvetli ju aj cez vedecké štúdie. Na základe toho mi povedz, v akej som dnes "
    "kondícii a či môžem trénovať naplno, alebo mám poľaviť. Daj mi krátke zhrnutie a "
    "jedno konkrétne odporúčanie na dnešok."
)

_COMMANDS = [
    BotCommand(command="help", description="How to use the bot"),
    BotCommand(command="daily_report", description="Get your daily health report now"),
    BotCommand(command="garmin_link", description="Link your Garmin account"),
    BotCommand(command="system_prompt", description="View/set/clear the system prompt"),
    BotCommand(command="tokens", description="Token usage and estimated cost for a month"),
]


async def _check_authorized(message: Message) -> int | None:
    if not message.from_user:
        return None

    telegram_id = message.from_user.id
    if not await database.is_authorized_user(telegram_id):
        logger.warning("Rejected message from unauthorized telegram_id=%s", telegram_id)
        await send_reply(message, "You don't have access to this bot.")
        return None

    return telegram_id


async def send_reply(message: Message, text: str) -> None:
    """Sends `text` formatted for Telegram (HTML, converted from Gemini's
    markdown-ish output). Falls back to plain text if Telegram ever rejects the
    conversion's output as malformed - a formatting bug should never block a reply
    from reaching the user."""
    try:
        await message.answer(to_telegram_html(text))
    except TelegramBadRequest:
        logger.warning("Telegram rejected HTML-formatted reply, falling back to plain text")
        await message.answer(text, parse_mode=None)


@router.message(Command("garmin_link"))
async def on_garmin_link(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    link_token = generate_link_token(telegram_id)
    url = f"{settings.telegram_webhook_base_url.rstrip('/')}/garmin-login?token={link_token}"
    await send_reply(message, f"Link your Garmin account here (link valid for 30 minutes):\n{url}")


@router.message(Command("daily_report"))
async def on_daily_report(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    reply = await handle_user_message(user_id=telegram_id, text=_DAILY_REPORT_PROMPT)
    await send_reply(message, reply)


@router.message(Command("system_prompt"))
async def on_system_prompt(message: Message, command: CommandObject) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    text = (command.args or "").strip()

    if not text:
        current = await database.get_system_prompt(telegram_id)
        if current:
            await send_reply(message, f"Current system prompt:\n\n{current}")
        else:
            await send_reply(
                message,
                "No system prompt is set. Set one like this:\n"
                "/system_prompt <text>\n\n"
                "E.g.: /system_prompt You are my personal fitness coach, I'm 196cm...\n\n"
                "Clear it with: /system_prompt clear",
            )
        return

    if text.lower() in {"clear", "reset"}:
        await database.set_system_prompt(telegram_id, None)
        await send_reply(message, "System prompt cleared.")
        return

    await database.set_system_prompt(telegram_id, text)
    await send_reply(message, "System prompt saved ✅ (applies from the next message)")


@router.message(Command("tokens"))
async def on_tokens(message: Message, command: CommandObject) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    arg = (command.args or "").strip()
    if arg:
        if not _MONTH_ARG_RE.match(arg):
            await send_reply(
                message,
                "Month format is YYYY-MM, e.g. /tokens 2026-08. Without an argument "
                "I'll show the current month.",
            )
            return
        year, month = int(arg[:4]), int(arg[5:7])
    else:
        now = datetime.now(UTC)
        year, month = now.year, now.month

    rows = await database.get_token_usage_summary(telegram_id, year, month)
    if not rows:
        await send_reply(message, f"No usage recorded for {year}-{month:02d}.")
        return

    lines = [f"Token usage for {year}-{month:02d}:"]
    total_calls = 0
    total_tokens = 0
    total_cost = 0.0
    any_unknown_pricing = False

    for row in rows:
        cost = token_pricing.estimate_cost_usd(row["model"], row["prompt_tokens"], row["response_tokens"])
        total_calls += row["call_count"]
        total_tokens += row["total_tokens"]
        if cost is None:
            any_unknown_pricing = True
            cost_str = "cost unknown"
        else:
            total_cost += cost
            cost_str = f"${cost:.4f}"
        lines.append(
            f"• {row['model']}: {row['call_count']} calls, "
            f"{row['prompt_tokens']} in + {row['response_tokens']} out = "
            f"{row['total_tokens']} tokens, {cost_str}"
        )

    summary = f"\nTotal: {total_calls} calls, {total_tokens} tokens"
    if total_cost > 0:
        summary += f", estimated cost ${total_cost:.4f}"
    if any_unknown_pricing:
        summary += " (pricing unknown for some models)"
    lines.append(summary)

    await send_reply(message, "\n".join(lines))


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    await send_reply(
        message,
        "You can just write me anything, e.g.:\n"
        "- 'I weighed myself today, 88kg' - I'll save it\n"
        "- 'What was my weight over the last month?' - I'll look up the history\n"
        "- 'Sync Garmin data for yesterday' - I'll sync Garmin\n\n"
        "Commands:\n"
        "/daily_report - get your daily health report now\n"
        "/garmin_link - link your Garmin account\n"
        "/system_prompt <text> - set your assistant persona "
        "(no text shows the current one, 'clear' removes it)\n"
        "/tokens [YYYY-MM] - token usage and estimated cost for a month "
        "(no argument means the current month)",
    )


@router.message()
async def on_message(message: Message) -> None:
    telegram_id = await _check_authorized(message)
    if telegram_id is None:
        return

    if not message.text:
        await send_reply(message, "I can only process text messages for now.")
        return

    reply = await handle_user_message(user_id=telegram_id, text=message.text)
    await send_reply(message, reply)


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
