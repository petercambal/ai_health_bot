from datetime import datetime

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

INSERT_HEALTH_RECORD = "insert_health_record"
GET_HEALTH_RECORDS = "get_health_records"


class InsertHealthRecordArgs(BaseModel):
    """Validates arguments Gemini returns for the insert_health_record tool call."""

    typ: str = Field(..., min_length=1, max_length=50)
    data: dict = Field(...)
    ts: datetime | None = None


class GetHealthRecordsArgs(BaseModel):
    """Validates arguments Gemini returns for the get_health_records tool call."""

    typ: str = Field(..., min_length=1, max_length=50)
    days_back: int = Field(..., ge=1, le=3650)


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
    ]
)

__all__ = [
    "GEMINI_TOOL",
    "GET_HEALTH_RECORDS",
    "INSERT_HEALTH_RECORD",
    "GetHealthRecordsArgs",
    "InsertHealthRecordArgs",
    "ValidationError",
]
