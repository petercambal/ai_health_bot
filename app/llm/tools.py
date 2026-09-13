from datetime import UTC, date, datetime, timedelta

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

INSERT_HEALTH_RECORD = "insert_health_record"
GET_HEALTH_RECORDS = "get_health_records"
SYNC_GARMIN_DAY = "sync_garmin_day"
SYNC_GARMIN_RANGE = "sync_garmin_range"
SEARCH_STUDIES = "search_scientific_studies"


class InsertHealthRecordArgs(BaseModel):
    """Validates arguments Gemini returns for the insert_health_record tool call."""

    typ: str = Field(..., min_length=1, max_length=50)
    data: dict = Field(...)
    ts: datetime | None = None


class GetHealthRecordsArgs(BaseModel):
    """Validates arguments Gemini returns for the get_health_records tool call."""

    typ: str = Field(..., min_length=1, max_length=50)
    days_back: int = Field(..., ge=1, le=3650)


class SyncGarminDayArgs(BaseModel):
    """Validates arguments Gemini returns for the sync_garmin_day tool call."""

    date: str = Field(..., min_length=1, max_length=20)


class SyncGarminRangeArgs(BaseModel):
    """Validates arguments Gemini returns for the sync_garmin_range tool call."""

    start_date: str = Field(..., min_length=1, max_length=20)
    end_date: str = Field(..., min_length=1, max_length=20)


class SearchStudiesArgs(BaseModel):
    """Validates arguments Gemini returns for the search_scientific_studies tool call."""

    query: str = Field(..., min_length=1, max_length=200)


def resolve_day(value: str) -> date | None:
    """Resolves 'today'/'yesterday' or an ISO date string to a date.

    Done in Python rather than trusted to the model's own date arithmetic.
    Returns None if the value can't be parsed.
    """
    normalized = value.strip().lower()
    today = datetime.now(UTC).date()
    if normalized == "today":
        return today
    if normalized == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


# Hand-written OpenAPI-subset schemas for the Gemini function declarations.
# Gemini's schema dialect doesn't support anyOf/$ref, so these are kept separate
# from the Pydantic models above (which validate the args Gemini sends back)
# rather than generated from them. Keep field names/types in sync by hand.
_INSERT_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "typ": types.Schema(
            type=types.Type.STRING,
            description=(
                "Health record category, e.g. 'weight', 'blood_pressure', "
                "'manual_workout', 'garmin_sleep'. Use short snake_case."
            ),
        ),
        "data": types.Schema(
            type=types.Type.OBJECT,
            description=(
                "Structured payload for this record. Shape depends on typ, e.g. "
                "{\"value_kg\": 82.5} for weight, {\"systolic\": 120, \"diastolic\": 80} "
                "for blood_pressure, {\"activity\": \"running\", \"duration_min\": 30} "
                "for manual_workout."
            ),
        ),
        "ts": types.Schema(
            type=types.Type.STRING,
            description="ISO 8601 timestamp of the record. Omit to use the current time.",
        ),
    },
    required=["typ", "data"],
)

_GET_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "typ": types.Schema(
            type=types.Type.STRING,
            description="Health record category to query, e.g. 'weight', 'manual_workout'.",
        ),
        "days_back": types.Schema(
            type=types.Type.INTEGER,
            description="How many days back to look, e.g. 7 for the last week.",
        ),
    },
    required=["typ", "days_back"],
)

_SYNC_GARMIN_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "date": types.Schema(
            type=types.Type.STRING,
            description=(
                "Which day to fetch from Garmin Connect: 'today', 'yesterday', or an "
                "ISO date YYYY-MM-DD."
            ),
        ),
    },
    required=["date"],
)

_SYNC_GARMIN_RANGE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "start_date": types.Schema(
            type=types.Type.STRING,
            description="First day of the range: 'today', 'yesterday', or an ISO date YYYY-MM-DD.",
        ),
        "end_date": types.Schema(
            type=types.Type.STRING,
            description="Last day of the range (inclusive), same format as start_date.",
        ),
    },
    required=["start_date", "end_date"],
)

_SEARCH_STUDIES_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "query": types.Schema(
            type=types.Type.STRING,
            description=(
                "Search keywords for peer-reviewed research on Semantic Scholar, e.g. "
                "'HRV sleep recovery' or 'VO2 max training longevity'. Derive these from "
                "the health topic the user is asking about, not their literal wording."
            ),
        ),
    },
    required=["query"],
)

GEMINI_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name=INSERT_HEALTH_RECORD,
            description=(
                "Store a new health data point reported by the user, such as a weight "
                "measurement, blood pressure reading, or workout."
            ),
            parameters=_INSERT_SCHEMA,
        ),
        types.FunctionDeclaration(
            name=GET_HEALTH_RECORDS,
            description=(
                "Fetch the user's previously stored health records of a given type "
                "for statistics, comparisons, or trend questions."
            ),
            parameters=_GET_SCHEMA,
        ),
        types.FunctionDeclaration(
            name=SYNC_GARMIN_DAY,
            description=(
                "Fetch/refresh this user's Garmin Connect data (steps, sleep, heart "
                "rate, activities, etc.) for a specific day and store it. Call this "
                "when the user asks to sync, fetch, or refresh Garmin data."
            ),
            parameters=_SYNC_GARMIN_SCHEMA,
        ),
        types.FunctionDeclaration(
            name=SYNC_GARMIN_RANGE,
            description=(
                "Fetch/refresh this user's Garmin Connect data for a range of days "
                "(inclusive), up to 31 days at a time. Call this when the user asks "
                "to sync, backfill, or initialize Garmin data for a period longer "
                "than a single day, e.g. 'sync my garmin data for the last month'."
            ),
            parameters=_SYNC_GARMIN_RANGE_SCHEMA,
        ),
        types.FunctionDeclaration(
            name=SEARCH_STUDIES,
            description=(
                "Search Semantic Scholar for peer-reviewed scientific studies on a "
                "health/longevity topic. Call this when discussing something worth "
                "grounding in current research (e.g. sleep, HRV, recovery, training "
                "load, VO2 max, nutrition, longevity). Always cite the author(s) and "
                "publication year of any study you reference in your answer."
            ),
            parameters=_SEARCH_STUDIES_SCHEMA,
        ),
    ]
)

__all__ = [
    "GEMINI_TOOL",
    "GET_HEALTH_RECORDS",
    "INSERT_HEALTH_RECORD",
    "SEARCH_STUDIES",
    "SYNC_GARMIN_DAY",
    "SYNC_GARMIN_RANGE",
    "GetHealthRecordsArgs",
    "InsertHealthRecordArgs",
    "SearchStudiesArgs",
    "SyncGarminDayArgs",
    "SyncGarminRangeArgs",
    "ValidationError",
    "resolve_day",
]
