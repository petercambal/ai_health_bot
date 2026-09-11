import logging
from pathlib import Path

from google.genai import types
from pydantic import ValidationError

from app import database
from app.config import settings
from app.llm.client import get_client
from app.llm.tools import (
    GEMINI_TOOL,
    GET_HEALTH_RECORDS,
    INSERT_HEALTH_RECORD,
    GetHealthRecordsArgs,
    InsertHealthRecordArgs,
)

logger = logging.getLogger(__name__)

# Fixed instruction that guarantees the three routing paths (direct answer / insert /
# query) keep working - do not move user-specific persona text in here, it belongs in
# system_prompt.txt (see below) so it can be edited without touching code.
_ROUTING_INSTRUCTION = (
    "You are a Telegram bot assistant backed by a personal health-tracking database, "
    "scoped to the current user. When the user reports a new measurement, workout, or "
    "other health data, call insert_health_record. When the user asks for statistics, "
    "trends, comparisons, or progress based on past data, call get_health_records. For "
    "anything else - general questions, advice, small talk - answer directly without "
    "calling a tool. Always respond in the language the user wrote in."
)

# User-editable persona/profile (e.g. "you are my fitness coach, I'm 196cm, my plan
# is..."), loaded as plain text so it never needs a code change or restart-less deploy.
# Kept out of git (see .gitignore) since it can contain personal health info; copy
# system_prompt.example.txt to system_prompt.txt and fill it in.
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.txt"


def _load_user_profile() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(
            "%s not found; running with routing instruction only. Copy "
            "system_prompt.example.txt to system_prompt.txt to add a persona.",
            _SYSTEM_PROMPT_PATH,
        )
        return ""


SYSTEM_INSTRUCTION = "\n\n".join(filter(None, [_ROUTING_INSTRUCTION, _load_user_profile()]))

_GENERATE_CONFIG = types.GenerateContentConfig(
    tools=[GEMINI_TOOL],
    system_instruction=SYSTEM_INSTRUCTION,
)

FALLBACK_ERROR_MESSAGE = (
    "Prepáč, nepodarilo sa mi to teraz spracovať. Skús to prosím preformulovať alebo "
    "to skús znova o chvíľu."
)


async def handle_user_message(user_id: int, text: str) -> str:
    client = get_client()
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=text)])
    ]

    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=_GENERATE_CONFIG,
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
    )


async def _dispatch_tool_call(
    client,
    user_id: int,
    contents: list[types.Content],
    model_turn: types.Content,
    function_call: types.FunctionCall,
) -> str:
    name = function_call.name
    raw_args = dict(function_call.args or {})

    if name == INSERT_HEALTH_RECORD:
        return await _handle_insert(user_id, raw_args)

    if name == GET_HEALTH_RECORDS:
        return await _handle_get(client, user_id, contents, model_turn, function_call, raw_args)

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
            config=_GENERATE_CONFIG,
        )
    except Exception:
        logger.exception(
            "Gemini follow-up generate_content call failed (user_id=%s)", user_id
        )
        return FALLBACK_ERROR_MESSAGE

    return response.text or FALLBACK_ERROR_MESSAGE
