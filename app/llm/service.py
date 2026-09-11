import logging

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
    GetHealthRecordsArgs,
    InsertHealthRecordArgs,
    SyncGarminDayArgs,
    resolve_day,
)

logger = logging.getLogger(__name__)

# Fixed instruction that guarantees the four routing paths (direct answer / insert /
# query / garmin sync) keep working - user-specific persona text (set via the
# /system_prompt Telegram command, stored in auth_user.system_prompt) is appended to
# this per-request in _build_config(), never merged into this constant.
_ROUTING_INSTRUCTION = (
    "You are a Telegram bot assistant backed by a personal health-tracking database, "
    "scoped to the current user. When the user reports a new measurement, workout, or "
    "other health data, call insert_health_record. When the user asks for statistics, "
    "trends, comparisons, or progress based on past data, call get_health_records. When "
    "the user asks to sync, fetch, or refresh Garmin data for a day, call "
    "sync_garmin_day. For anything else - general questions, advice, small talk - "
    "answer directly without calling a tool. Always respond in the language the user "
    "wrote in."
)

FALLBACK_ERROR_MESSAGE = (
    "Prepáč, nepodarilo sa mi to teraz spracovať. Skús to prosím preformulovať alebo "
    "to skús znova o chvíľu."
)


async def _build_config(user_id: int) -> types.GenerateContentConfig:
    """Builds the per-request Gemini config: fixed routing instruction + this user's
    own persona/profile text (auth_user.system_prompt, set via /system_prompt),
    fetched fresh on every call so an update takes effect immediately."""
    user_prompt = await database.get_system_prompt(user_id)
    instruction = "\n\n".join(filter(None, [_ROUTING_INSTRUCTION, (user_prompt or "").strip()]))
    return types.GenerateContentConfig(tools=[GEMINI_TOOL], system_instruction=instruction)


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
            "Nerozumel som presne, čo mám uložiť. Skús to prosím napísať inak "
            "(napr. typ merania a hodnotu)."
        )

    try:
        await database.insert_health_record(
            user_id=user_id, typ=args.typ, data=args.data, ts=args.ts
        )
    except Exception:
        logger.exception("Failed to insert health record (user_id=%s)", user_id)
        return FALLBACK_ERROR_MESSAGE

    return f"Uložené ✅ ({args.typ})"


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
            "Nerozumel som presne, aké dáta chceš zobraziť. Skús napísať typ merania "
            "a časové obdobie (napr. 'váha za posledných 30 dní')."
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
            "Nerozumel som, za aký deň mám Garmin dáta stiahnuť. Skús napr. 'včera' "
            "alebo dátum v tvare RRRR-MM-DD."
        )

    day = resolve_day(args.date)
    if day is None:
        return "Nerozumel som dátumu. Skús napr. 'dnes', 'včera' alebo formát RRRR-MM-DD."

    try:
        await garmin_sync.sync_day(user_id=user_id, day=day)
    except GarminAuthRequired as e:
        logger.warning("Garmin auth required (user_id=%s): %s", user_id, e)
        return str(e)
    except Exception:
        logger.exception("Garmin sync failed (user_id=%s, day=%s)", user_id, day)
        return FALLBACK_ERROR_MESSAGE

    return f"Garmin dáta za {day.isoformat()} stiahnuté a uložené ✅"
