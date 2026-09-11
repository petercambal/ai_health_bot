import logging
from datetime import UTC, datetime

from google.genai import types
from pydantic import ValidationError

from app import database
from app.config import settings
from app.garmin import sync as garmin_sync
from app.garmin.sync import GarminAuthRequired
from app.llm.client import get_client
from app.llm.tools import (
    GEMINI_TOOL,
    GET_HEALTH_RECORDS,
    INSERT_HEALTH_RECORD,
    SYNC_GARMIN_DAY,
    SYNC_GARMIN_RANGE,
    GetHealthRecordsArgs,
    InsertHealthRecordArgs,
    SyncGarminDayArgs,
    SyncGarminRangeArgs,
    resolve_day,
)

logger = logging.getLogger(__name__)

def _routing_instruction() -> str:
    """Fixed instruction that guarantees the five routing paths (direct answer /
    insert / query / garmin day sync / garmin range sync) keep working - user-specific
    persona text (set via /system_prompt, stored in auth_user.system_prompt) is
    appended to this per-request in _build_config(), never merged in here.

    Built fresh per request (not a module-level constant) specifically to stamp in
    today's real date - without it, the model has no ground truth for "today" and
    silently invents one (observed: resolving "since the start of September" to
    2024-09-01 instead of the actual current year) when computing start_date/end_date
    for sync_garmin_range from a relative phrase."""
    today = datetime.now(UTC).date().isoformat()
    return (
        f"Today's date is {today}. Use this as ground truth for any relative date or "
        "date range the user mentions (e.g. 'yesterday', 'this month', 'since the "
        "start of September') - never guess or assume a different year. "
        "You are a Telegram bot assistant backed by a personal health-tracking database, "
        "scoped to the current user. When the user reports a new measurement, workout, or "
        "other health data, call insert_health_record. When the user asks for statistics, "
        "trends, comparisons, or progress based on past data, call get_health_records. When "
        "the user asks to sync, fetch, or refresh Garmin data for a single day, call "
        "sync_garmin_day; for a range of days (e.g. backfilling history), call "
        f"sync_garmin_range (max {garmin_sync.MAX_RANGE_DAYS} days at a time - if the user "
        "asks for a longer period, sync the most recent portion and tell them to ask again "
        "for the rest). For anything else - general questions, advice, small talk - answer "
        "directly without calling a tool. Always respond in the language the user wrote in."
    )

FALLBACK_ERROR_MESSAGE = (
    "Sorry, I couldn't process that right now. Please try rephrasing it or try "
    "again in a moment."
)


async def _build_config(user_id: int) -> types.GenerateContentConfig:
    """Builds the per-request Gemini config: fixed routing instruction + this user's
    own persona/profile text (auth_user.system_prompt, set via /system_prompt),
    fetched fresh on every call so an update takes effect immediately."""
    user_prompt = await database.get_system_prompt(user_id)
    instruction = "\n\n".join(filter(None, [_routing_instruction(), (user_prompt or "").strip()]))
    return types.GenerateContentConfig(tools=[GEMINI_TOOL], system_instruction=instruction)


async def _log_usage(user_id: int, response: types.GenerateContentResponse) -> None:
    """Records this Gemini call's token cost for the /token_usage audit log.
    Best-effort - a logging failure must never break the actual bot reply."""
    usage = response.usage_metadata
    if usage is None:
        return
    try:
        await database.log_token_usage(
            user_id=user_id,
            model=settings.gemini_model,
            prompt_tokens=usage.prompt_token_count,
            response_tokens=usage.candidates_token_count,
            total_tokens=usage.total_token_count,
        )
    except Exception:
        logger.exception("Failed to log token usage (user_id=%s)", user_id)


async def handle_user_message(user_id: int, text: str) -> str:
    client = get_client()
    config = await _build_config(user_id)
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=text)])
    ]

    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )
    except Exception:
        logger.exception("Gemini generate_content call failed (user_id=%s)", user_id)
        return FALLBACK_ERROR_MESSAGE

    await _log_usage(user_id, response)

    candidate = response.candidates[0] if response.candidates else None
    parts = candidate.content.parts if candidate and candidate.content else []
    function_call = next((p.function_call for p in parts if p.function_call), None)

    if function_call is None:
        return response.text or FALLBACK_ERROR_MESSAGE

    return await _dispatch_tool_call(
        client=client,
        user_id=user_id,
        contents=contents,
        model_turn=candidate.content,
        function_call=function_call,
        config=config,
    )


async def _dispatch_tool_call(
    client,
    user_id: int,
    contents: list[types.Content],
    model_turn: types.Content,
    function_call: types.FunctionCall,
    config: types.GenerateContentConfig,
) -> str:
    name = function_call.name
    raw_args = dict(function_call.args or {})

    if name == INSERT_HEALTH_RECORD:
        return await _handle_insert(user_id, raw_args)

    if name == GET_HEALTH_RECORDS:
        return await _handle_get(client, user_id, contents, model_turn, function_call, raw_args, config)

    if name == SYNC_GARMIN_DAY:
        return await _handle_sync_garmin(user_id, raw_args)

    if name == SYNC_GARMIN_RANGE:
        return await _handle_sync_garmin_range(user_id, raw_args)

    logger.warning("Gemini called unknown tool %r (user_id=%s)", name, user_id)
    return FALLBACK_ERROR_MESSAGE


async def _handle_insert(user_id: int, raw_args: dict) -> str:
    try:
        args = InsertHealthRecordArgs.model_validate(raw_args)
    except ValidationError:
        logger.warning(
            "Invalid insert_health_record args from Gemini (user_id=%s): %r",
            user_id,
            raw_args,
        )
        return (
            "I wasn't quite sure what to save. Please try phrasing it differently "
            "(e.g. the measurement type and value)."
        )

    try:
        await database.insert_health_record(
            user_id=user_id, typ=args.typ, data=args.data, ts=args.ts
        )
    except Exception:
        logger.exception("Failed to insert health record (user_id=%s)", user_id)
        return FALLBACK_ERROR_MESSAGE

    return f"Saved ✅ ({args.typ})"


async def _handle_get(
    client,
    user_id: int,
    contents: list[types.Content],
    model_turn: types.Content,
    function_call: types.FunctionCall,
    raw_args: dict,
    config: types.GenerateContentConfig,
) -> str:
    try:
        args = GetHealthRecordsArgs.model_validate(raw_args)
    except ValidationError:
        logger.warning(
            "Invalid get_health_records args from Gemini (user_id=%s): %r",
            user_id,
            raw_args,
        )
        return (
            "I wasn't quite sure what data you want to see. Try naming the "
            "measurement type and a time period (e.g. 'weight over the last 30 days')."
        )

    try:
        records = await database.get_health_records(
            user_id=user_id, typ=args.typ, days_back=args.days_back
        )
    except Exception:
        logger.exception("Failed to fetch health records (user_id=%s)", user_id)
        return FALLBACK_ERROR_MESSAGE

    contents.append(model_turn)
    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name=function_call.name,
                    response={"records": records},
                )
            ],
        )
    )

    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )
    except Exception:
        logger.exception(
            "Gemini follow-up generate_content call failed (user_id=%s)", user_id
        )
        return FALLBACK_ERROR_MESSAGE

    await _log_usage(user_id, response)

    return response.text or FALLBACK_ERROR_MESSAGE


async def _handle_sync_garmin(user_id: int, raw_args: dict) -> str:
    try:
        args = SyncGarminDayArgs.model_validate(raw_args)
    except ValidationError:
        logger.warning(
            "Invalid sync_garmin_day args from Gemini (user_id=%s): %r",
            user_id,
            raw_args,
        )
        return (
            "I wasn't sure which day to fetch Garmin data for. Try e.g. 'yesterday' "
            "or a date in YYYY-MM-DD format."
        )

    day = resolve_day(args.date)
    if day is None:
        return "I didn't understand the date. Try e.g. 'today', 'yesterday', or YYYY-MM-DD."

    try:
        await garmin_sync.sync_day(user_id=user_id, day=day)
    except GarminAuthRequired as e:
        logger.warning("Garmin auth required (user_id=%s): %s", user_id, e)
        return str(e)
    except Exception:
        logger.exception("Garmin sync failed (user_id=%s, day=%s)", user_id, day)
        return FALLBACK_ERROR_MESSAGE

    return f"Garmin data for {day.isoformat()} fetched and saved ✅"


async def _handle_sync_garmin_range(user_id: int, raw_args: dict) -> str:
    try:
        args = SyncGarminRangeArgs.model_validate(raw_args)
    except ValidationError:
        logger.warning(
            "Invalid sync_garmin_range args from Gemini (user_id=%s): %r",
            user_id,
            raw_args,
        )
        return (
            "I wasn't sure about the date range. Try e.g. 'from 2026-08-01 to "
            "2026-08-31' or 'last month'."
        )

    start = resolve_day(args.start_date)
    end = resolve_day(args.end_date)
    if start is None or end is None:
        return "I didn't understand the dates. Try e.g. 'today', 'yesterday', or YYYY-MM-DD."

    span_days = abs((end - start).days) + 1
    if span_days > garmin_sync.MAX_RANGE_DAYS:
        return (
            f"A {span_days}-day range is too long, the max is {garmin_sync.MAX_RANGE_DAYS} "
            "days at a time. Try splitting it into shorter periods."
        )

    try:
        result = await garmin_sync.sync_range(user_id=user_id, start=start, end=end)
    except GarminAuthRequired as e:
        logger.warning("Garmin auth required (user_id=%s): %s", user_id, e)
        return str(e)
    except Exception:
        logger.exception(
            "Garmin range sync failed (user_id=%s, start=%s, end=%s)", user_id, start, end
        )
        return FALLBACK_ERROR_MESSAGE

    ok_days = [d for d, v in result.items() if v is not None]
    failed_days = [d for d, v in result.items() if v is None]

    summary = f"Garmin data fetched: {len(ok_days)}/{len(result)} days ({start.isoformat()} - {end.isoformat()}) ✅"
    if failed_days:
        failed_str = ", ".join(d.isoformat() for d in sorted(failed_days))
        summary += f"\nFailed: {failed_str}"
    return summary
