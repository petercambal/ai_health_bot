import logging
from datetime import UTC, datetime

from google.genai import types
from pydantic import ValidationError

from app import database
from app.config import settings
from app.garmin import sync as garmin_sync
from app.garmin.sync import GarminAuthRequired
from app.llm import studies
from app.llm.client import get_client
from app.llm.tools import (
    GEMINI_TOOL,
    GET_HEALTH_RECORDS,
    GET_LAST_WORKOUT,
    INSERT_HEALTH_RECORD,
    LOG_WORKOUT_SESSION,
    SEARCH_STUDIES,
    SYNC_GARMIN_DAY,
    SYNC_GARMIN_RANGE,
    GetHealthRecordsArgs,
    GetLastWorkoutArgs,
    InsertHealthRecordArgs,
    LogWorkoutSessionArgs,
    SearchStudiesArgs,
    SyncGarminDayArgs,
    SyncGarminRangeArgs,
    resolve_day,
)

# Fast-path-eligible tools: single-action calls that produce a deterministic
# confirmation without needing the model to see/synthesize results, so they skip the
# follow-up Gemini round-trip in _dispatch_tool_calls (see there). Deliberately an
# allowlist, not "anything but get_health_records" - a bare exclusion would silently
# fast-path any newly added query-style tool (e.g. search_scientific_studies) into
# the wrong branch, where it has no case and falls through to "unknown tool".
_FAST_PATH_TOOLS = frozenset(
    {INSERT_HEALTH_RECORD, SYNC_GARMIN_DAY, SYNC_GARMIN_RANGE, LOG_WORKOUT_SESSION}
)

logger = logging.getLogger(__name__)


def _routing_instruction(known_types: list[str]) -> str:
    """Fixed instruction that guarantees the six routing paths (direct answer /
    insert / query / garmin day sync / garmin range sync / study search) keep
    working - user-specific persona text (set via /system_prompt, stored in
    auth_user.system_prompt) is appended to this per-request in _build_config(),
    never merged in here.

    Built fresh per request (not a module-level constant) for two reasons:
    1. Stamps in today's real date - without it, the model has no ground truth for
       "today" and silently invents one (observed: resolving "since the start of
       September" to 2024-09-01 instead of the actual current year).
    2. Lists this user's actual recorded record types. Without this, a broad request
       (e.g. "analyze my August") makes the model guess at plausible-sounding types
       ('sleep', 'activity', 'steps', 'blood_pressure', ...) one at a time via
       get_health_records, most of which don't exist for this user and return
       empty - observed running for several rounds of parallel calls before the
       model would even attempt a text answer."""
    today = datetime.now(UTC).date().isoformat()
    types_note = (
        f"This user's existing record types are: {', '.join(known_types)}. Prefer "
        "these over guessing a new type name when querying with get_health_records."
        if known_types
        else "This user has no recorded data yet."
    )
    return (
        f"Today's date is {today}. Use this as ground truth for any relative date or "
        "date range the user mentions (e.g. 'yesterday', 'this month', 'since the "
        "start of September') - never guess or assume a different year. "
        f"{types_note} "
        "You are a Telegram bot assistant backed by a personal health-tracking database, "
        "scoped to the current user. When the user reports a new measurement, workout, or "
        "other health data, call insert_health_record. When the user asks for statistics, "
        "trends, comparisons, or progress based on past data, call get_health_records. When "
        "the user asks to sync, fetch, or refresh Garmin data for a single day, call "
        "sync_garmin_day; for a range of days (e.g. backfilling history), call "
        f"sync_garmin_range (max {garmin_sync.MAX_RANGE_DAYS} days at a time - if the user "
        "asks for a longer period, sync the most recent portion and tell them to ask again "
        "for the rest). When the topic would benefit from current research (see your "
        "persona instructions below for when that applies), call search_scientific_studies. "
        "For anything else - general questions, advice, small talk - answer directly "
        "without calling a tool. Always respond in the language the user wrote in."
    )


# Shared persona layer, applied to every user before their own /system_prompt (set
# via auth_user.system_prompt) is appended in _build_config(). Edit this to change
# the assistant's baseline character/expertise for everyone at once; per-user
# specifics (stats, goals, training plan) belong in /system_prompt instead, not here.
_BASE_PERSONA = (
    "You are a longevity expert and coach. You analyze health data collected from "
    "multiple sources (this user's manually logged records and their Garmin data) to "
    "give grounded, practical guidance on healthspan, recovery, and performance. When "
    "a question touches a topic worth backing with current research - sleep, HRV, "
    "recovery, training load, VO2 max, nutrition, longevity interventions, and "
    "similar - identify the relevant scientific keywords (e.g. a sleep question maps "
    "to keywords like 'HRV' and 'sleep architecture') and call "
    "search_scientific_studies with them before answering. When you cite a study, "
    "always name the author(s) and publication year, and weave the citation into the "
    "answer naturally rather than just listing references at the end. If no relevant "
    "studies come back, say so plainly and answer from general expertise instead of "
    "inventing a citation."
)


# Caps the multi-call synthesis loop in _dispatch_tool_calls (see its docstring for
# why this is a loop, not a single follow-up call). Each round is one Gemini call, so
# this also bounds the worst-case cost/quota use of one Telegram message. Requests
# that touch many data types plus several study searches (e.g. /daily_report) can
# need more than a couple of rounds - if this is ever hit, _dispatch_tool_calls still
# forces one final text-only call rather than giving up (see the end of that loop).
_MAX_TOOL_ROUNDS = 4

FALLBACK_ERROR_MESSAGE = (
    "Sorry, I couldn't process that right now. Please try rephrasing it or try "
    "again in a moment."
)


async def _build_config(user_id: int) -> types.GenerateContentConfig:
    """Builds the per-request Gemini config, three layers concatenated: the fixed
    routing instruction, the shared longevity-coach persona (_BASE_PERSONA), and this
    user's own profile text (auth_user.system_prompt, set via /system_prompt) -
    fetched fresh on every call so a /system_prompt update takes effect immediately."""
    user_prompt = await database.get_system_prompt(user_id)
    known_types = await database.get_distinct_types(user_id)
    instruction = "\n\n".join(
        filter(
            None,
            [_routing_instruction(known_types), _BASE_PERSONA, (user_prompt or "").strip()],
        )
    )
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
    function_calls = [p.function_call for p in parts if p.function_call]

    if not function_calls:
        return response.text or FALLBACK_ERROR_MESSAGE

    return await _dispatch_tool_calls(
        client=client,
        user_id=user_id,
        contents=contents,
        model_turn=candidate.content,
        function_calls=function_calls,
        config=config,
    )


async def _dispatch_tool_calls(
    client,
    user_id: int,
    contents: list[types.Content],
    model_turn: types.Content,
    function_calls: list[types.FunctionCall],
    config: types.GenerateContentConfig,
) -> str:
    # Fast path: a single non-query action call keeps the original one-Gemini-call
    # behavior - a plain confirmation doesn't need a synthesis round-trip, and
    # skipping it matters given free-tier request quotas.
    if len(function_calls) == 1 and function_calls[0].name in _FAST_PATH_TOOLS:
        fc = function_calls[0]
        raw_args = dict(fc.args or {})
        if fc.name == INSERT_HEALTH_RECORD:
            return await _handle_insert(user_id, raw_args)
        if fc.name == SYNC_GARMIN_DAY:
            return await _handle_sync_garmin(user_id, raw_args)
        if fc.name == SYNC_GARMIN_RANGE:
            return await _handle_sync_garmin_range(user_id, raw_args)
        if fc.name == LOG_WORKOUT_SESSION:
            return await _handle_log_workout_session(user_id, raw_args)
        logger.warning("Gemini called unknown tool %r (user_id=%s)", fc.name, user_id)
        return FALLBACK_ERROR_MESSAGE

    # General path: one or more calls that need the model to see structured results
    # and synthesize a final reply. get_health_records always needs this; so does any
    # turn with more than one call - e.g. some models issue several parallel
    # get_health_records calls (one per data type) for a broad "analyze my month"
    # request, and every function_call in a turn needs a matching function_response
    # before the conversation can continue, so they're all executed here.
    #
    # Bounded loop, not a single follow-up call: passing tool_config with
    # mode=NONE ("model will not predict any function calls") to force a text-only
    # reply on the follow-up turn was tried and did NOT work reliably (observed with
    # gemini-3.6-flash still returning function_calls despite it), so instead this
    # keeps answering whatever calls come back, up to _MAX_TOOL_ROUNDS, and accepts
    # whatever text exists after that rather than looping forever.
    contents.append(model_turn)
    current_calls = function_calls
    response: types.GenerateContentResponse | None = None

    for _round in range(_MAX_TOOL_ROUNDS):
        response_parts = []
        for fc in current_calls:
            result = await _execute_for_synthesis(user_id, fc)
            response_parts.append(
                types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=result))
            )
        contents.append(types.Content(role="user", parts=response_parts))

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

        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else []
        current_calls = [p.function_call for p in parts if p.function_call]

        if not current_calls:
            return response.text or FALLBACK_ERROR_MESSAGE

        contents.append(candidate.content)

    # Hit the round cap with the model still wanting to call more tools. Execute
    # those last pending calls too rather than discarding their results, then force
    # a genuinely text-only reply: passing a config with no `tools` declared at all
    # guarantees this, since the model has nothing left it *can* call - the
    # tool_config mode=NONE approach tried earlier only asked the model not to call
    # anything and it ignored that, so this leaves it no other option.
    logger.warning(
        "Hit _MAX_TOOL_ROUNDS (%d), forcing a text-only synthesis call (user_id=%s)",
        _MAX_TOOL_ROUNDS,
        user_id,
    )
    response_parts = []
    for fc in current_calls:
        result = await _execute_for_synthesis(user_id, fc)
        response_parts.append(
            types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=result))
        )
    contents.append(types.Content(role="user", parts=response_parts))
    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Based on everything gathered so far, give your final answer now - "
                        "do not request any more data."
                    )
                )
            ],
        )
    )

    final_config = types.GenerateContentConfig(system_instruction=config.system_instruction)
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model, contents=contents, config=final_config
        )
    except Exception:
        logger.exception("Gemini forced-synthesis call failed (user_id=%s)", user_id)
        return FALLBACK_ERROR_MESSAGE

    await _log_usage(user_id, response)
    return response.text or (
        "I gathered some data but couldn't finish putting together an answer. "
        "Try asking about a narrower period or a specific metric."
    )


async def _execute_for_synthesis(user_id: int, function_call: types.FunctionCall) -> dict:
    """Executes one tool call for the multi-call/synthesis path in _dispatch_tool_calls
    and returns a JSON-able dict to feed back to the model as that call's
    function_response. Never raises - errors become a structured payload so the model
    can explain the failure in its own words instead of the turn just failing."""
    name = function_call.name
    raw_args = dict(function_call.args or {})

    if name == INSERT_HEALTH_RECORD:
        try:
            args = InsertHealthRecordArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid insert_health_record args from Gemini (user_id=%s): %r", user_id, raw_args
            )
            return {"error": "invalid arguments"}
        try:
            await database.insert_health_record(
                user_id=user_id, typ=args.typ, data=args.data, ts=args.ts
            )
        except Exception:
            logger.exception("Failed to insert health record (user_id=%s)", user_id)
            return {"error": "failed to save"}
        return {"status": "saved", "typ": args.typ}

    if name == GET_HEALTH_RECORDS:
        try:
            args = GetHealthRecordsArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid get_health_records args from Gemini (user_id=%s): %r", user_id, raw_args
            )
            return {"error": "invalid arguments"}
        try:
            records = await database.get_health_records(
                user_id=user_id, typ=args.typ, days_back=args.days_back
            )
        except Exception:
            logger.exception("Failed to fetch health records (user_id=%s)", user_id)
            return {"error": "failed to fetch records"}
        return {"records": records}

    if name == SYNC_GARMIN_DAY:
        try:
            args = SyncGarminDayArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid sync_garmin_day args from Gemini (user_id=%s): %r", user_id, raw_args
            )
            return {"error": "invalid arguments"}
        day = resolve_day(args.date)
        if day is None:
            return {"error": "could not parse date"}
        try:
            await garmin_sync.sync_day(user_id=user_id, day=day)
        except GarminAuthRequired as e:
            logger.warning("Garmin auth required (user_id=%s): %s", user_id, e)
            return {"error": str(e)}
        except Exception:
            logger.exception("Garmin sync failed (user_id=%s, day=%s)", user_id, day)
            return {"error": "garmin sync failed"}
        return {"status": "synced", "day": day.isoformat()}

    if name == SYNC_GARMIN_RANGE:
        try:
            args = SyncGarminRangeArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid sync_garmin_range args from Gemini (user_id=%s): %r", user_id, raw_args
            )
            return {"error": "invalid arguments"}
        start = resolve_day(args.start_date)
        end = resolve_day(args.end_date)
        if start is None or end is None:
            return {"error": "could not parse dates"}
        span_days = abs((end - start).days) + 1
        if span_days > garmin_sync.MAX_RANGE_DAYS:
            return {"error": f"range too long, max {garmin_sync.MAX_RANGE_DAYS} days at a time"}
        try:
            result = await garmin_sync.sync_range(user_id=user_id, start=start, end=end)
        except GarminAuthRequired as e:
            logger.warning("Garmin auth required (user_id=%s): %s", user_id, e)
            return {"error": str(e)}
        except Exception:
            logger.exception(
                "Garmin range sync failed (user_id=%s, start=%s, end=%s)", user_id, start, end
            )
            return {"error": "garmin range sync failed"}
        ok_days = [d for d, v in result.items() if v is not None]
        failed_days = [d for d, v in result.items() if v is None]
        return {
            "synced_days": len(ok_days),
            "total_days": len(result),
            "failed_days": [d.isoformat() for d in sorted(failed_days)],
        }

    if name == SEARCH_STUDIES:
        try:
            args = SearchStudiesArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid search_scientific_studies args from Gemini (user_id=%s): %r",
                user_id,
                raw_args,
            )
            return {"error": "invalid arguments"}
        results = await studies.search_studies(args.query)
        if not results:
            return {"studies": [], "note": "no studies found or search unavailable right now"}
        return {"studies": results}

    if name == GET_LAST_WORKOUT:
        try:
            args = GetLastWorkoutArgs.model_validate(raw_args)
        except ValidationError:
            logger.warning(
                "Invalid get_last_workout args from Gemini (user_id=%s): %r", user_id, raw_args
            )
            return {"error": "invalid arguments"}
        workout = await database.get_last_workout(user_id=user_id, variant=args.variant)
        if workout is None:
            return {"note": f"no previous workout logged for {args.variant!r}"}
        return {"last_workout": workout}

    if name == LOG_WORKOUT_SESSION:
        result = await _save_workout_session(user_id, raw_args)
        if result is None:
            return {"error": "invalid arguments"}
        return {"status": "saved", "variant": result.variant, "exercise_count": len(result.cviky)}

    logger.warning("Gemini called unknown tool %r (user_id=%s)", name, user_id)
    return {"error": f"unknown tool {name!r}"}


async def _save_workout_session(user_id: int, raw_args: dict) -> LogWorkoutSessionArgs | None:
    """Validates and stores a log_workout_session call as one health_records row
    (typ='workout', one row per session - see database.get_last_workout). Shared by
    the fast path and the multi-call synthesis path so both save identically.
    Returns None (rather than raising) on invalid args, matching the other
    _execute_for_synthesis-style handlers."""
    try:
        args = LogWorkoutSessionArgs.model_validate(raw_args)
    except ValidationError:
        logger.warning(
            "Invalid log_workout_session args from Gemini (user_id=%s): %r", user_id, raw_args
        )
        return None

    data = {
        "variant": args.variant,
        "cviky": [c.model_dump(exclude_none=True) for c in args.cviky],
    }
    try:
        await database.insert_health_record(user_id=user_id, typ="workout", data=data, ts=args.ts)
    except Exception:
        logger.exception("Failed to save workout session (user_id=%s)", user_id)
        return None
    return args


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


async def _handle_log_workout_session(user_id: int, raw_args: dict) -> str:
    args = await _save_workout_session(user_id, raw_args)
    if args is None:
        return (
            "I wasn't quite sure how to parse that workout summary. Please include "
            "the variant and each exercise with its weight/reps."
        )
    return f"Workout saved ✅ ({args.variant}, {len(args.cviky)} cvikov)"
