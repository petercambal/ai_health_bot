import pytest

from app.nutrition.sync import _parse_diary, _parse_energy, _unwrap_response

_SAMPLE_DIARY = [
    {
        "id": "1",
        "title": "Raňajky",
        "foodstuff": [
            {
                "title": "káva espresso",
                "unit": "150 ml",
                "energy": "6,6",
                "protein": 0.3,
                "carbohydrate": 0,
                "fat": 0.6,
                "fiber": 0,
                "sugar": 0,
            },
            {
                "title": "Chocapic celozrnné cereálie Nestlé",
                "unit": "veľká porcia (50 g)",
                "energy": "195",
                "protein": 4.3,
                "carbohydrate": 36.85,
                "fat": 2.35,
                "fiber": 3.75,
                "sugar": 12.45,
            },
        ],
        "energyTotal": "201,6",
    },
    {"id": "2", "title": "Desiata", "foodstuff": [], "energyTotal": None},
    {
        "id": "3",
        "title": "Obed",
        "foodstuff": [
            {
                "title": "špagety boloňské so syrom",
                "unit": "porcia (200 g)",
                "energy": "404",
                "protein": 17.8,
                "carbohydrate": 42,
                "fat": 18,
                "fiber": 2,
                "sugar": 5,
            },
        ],
        "energyTotal": "404",
    },
    {"id": "4", "title": "Olovrant", "foodstuff": [], "energyTotal": None},
]


def test_parse_energy_handles_slovak_decimal_comma():
    assert _parse_energy("6,6") == 6.6
    assert _parse_energy("404") == 404.0
    assert _parse_energy(12.5) == 12.5
    assert _parse_energy(None) is None


def test_parse_diary_skips_empty_meals():
    result = _parse_diary(_SAMPLE_DIARY)
    meal_names = [m["nazov"] for m in result["jedla"]]
    assert meal_names == ["Raňajky", "Obed"]


def test_parse_diary_day_totals():
    result = _parse_diary(_SAMPLE_DIARY)
    assert result["energy_kcal"] == 605.6
    assert result["protein_g"] == 22.4
    assert round(result["carbs_g"], 2) == 78.85
    assert round(result["fat_g"], 2) == 20.95


def test_parse_diary_food_item_detail():
    result = _parse_diary(_SAMPLE_DIARY)
    lunch = next(m for m in result["jedla"] if m["nazov"] == "Obed")
    assert lunch["kcal"] == 404.0
    assert lunch["polozky"] == [
        {
            "nazov": "špagety boloňské so syrom",
            "mnozstvo": "porcia (200 g)",
            "kcal": 404.0,
            "protein_g": 17.8,
            "carbs_g": 42,
            "fat_g": 18,
            "fiber_g": 2,
            "sugar_g": 5,
        }
    ]


def test_unwrap_response_extracts_times():
    envelope = {"requestId": None, "code": 0, "message": None, "data": {"date": 1789336800000, "times": _SAMPLE_DIARY}}
    assert _unwrap_response(envelope) == _SAMPLE_DIARY


def test_unwrap_response_missing_data_returns_empty():
    assert _unwrap_response({"code": 0, "data": None}) == []


def test_unwrap_response_error_code_raises():
    with pytest.raises(ValueError, match="error code=1"):
        _unwrap_response({"code": 1, "message": "not logged in", "data": None})
