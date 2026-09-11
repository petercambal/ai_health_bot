from datetime import UTC, datetime, timedelta

import pytest

from app.llm.tools import (
    GetHealthRecordsArgs,
    InsertHealthRecordArgs,
    SyncGarminDayArgs,
    ValidationError,
    resolve_day,
)


def test_insert_args_valid():
    args = InsertHealthRecordArgs.model_validate({"typ": "weight", "data": {"value_kg": 82.5}})
    assert args.typ == "weight"
    assert args.data == {"value_kg": 82.5}
    assert args.ts is None


def test_insert_args_missing_data_rejected():
    with pytest.raises(ValidationError):
        InsertHealthRecordArgs.model_validate({"typ": "weight"})


def test_get_args_valid():
    args = GetHealthRecordsArgs.model_validate({"typ": "weight", "days_back": 30})
    assert args.days_back == 30


def test_get_args_rejects_bad_days_back():
    with pytest.raises(ValidationError):
        GetHealthRecordsArgs.model_validate({"typ": "weight", "days_back": 0})


def test_sync_garmin_args_valid():
    args = SyncGarminDayArgs.model_validate({"date": "yesterday"})
    assert args.date == "yesterday"


def test_resolve_day_today_and_yesterday():
    today = datetime.now(UTC).date()
    assert resolve_day("today") == today
    assert resolve_day("Yesterday") == today - timedelta(days=1)


def test_resolve_day_iso_format():
    assert resolve_day("2026-01-15").isoformat() == "2026-01-15"


def test_resolve_day_invalid_returns_none():
    assert resolve_day("next tuesday") is None
