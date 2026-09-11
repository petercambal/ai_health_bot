import pytest

from app.llm.tools import GetHealthRecordsArgs, InsertHealthRecordArgs, ValidationError


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
