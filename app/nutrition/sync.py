"""Syncs daily nutrition data from kaloricketabulky.sk's (undocumented) diary
endpoint - the user logs food into that site's own UI, which has a far better food
database (including local brands) than this app could realistically build, and this
just pulls the resulting per-day breakdown into health_records (typ='nutrition', one
row per day - see database.replace_health_record_for_day).

No official API/export exists, so this replays the same private endpoint the site's
own frontend calls, authenticated with a stored cookie jar (see app/nutrition/web_login.py
for how that's obtained - never a stored password, same property as the Garmin
integration in app/garmin/).
"""

import logging
from datetime import UTC, date, datetime, timedelta

import aiohttp

from app import database

logger = logging.getLogger(__name__)

RECORD_TYPE = "nutrition"
INITIAL_BACKFILL_DAYS = 14
MAX_RANGE_DAYS = 31

_DIARY_URL = "https://www.kaloricketabulky.sk/user/diary/{date}/get"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class NutritionAuthRequired(Exception):
    """No usable kaloricketabulky.sk session for this user - they need to (re-)link
    via /nutrition_link."""


def _parse_energy(value: object) -> float | None:
    """The diary's `energy`/`energyTotal` fields come back as Slovak-locale strings
    with a comma decimal separator (e.g. "6,6"), unlike the other macro fields which
    are plain numbers - this normalizes either form."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def _parse_item(item: dict) -> dict:
    fields = {
        "nazov": item.get("title"),
        "mnozstvo": item.get("unit"),
        "kcal": _parse_energy(item.get("energy")),
        "protein_g": item.get("protein"),
        "carbs_g": item.get("carbohydrate"),
        "fat_g": item.get("fat"),
        "fiber_g": item.get("fiber"),
        "sugar_g": item.get("sugar"),
    }
    return {k: v for k, v in fields.items() if v is not None}


def _unwrap_response(envelope: dict) -> list[dict]:
    """The diary endpoint wraps its payload in {"code": 0, "data": {"times": [...]}}
    rather than returning the meal list directly (confirmed against a live response -
    the example captured from the browser devtools was apparently already the
    unwrapped `data.times` value). A non-zero `code` signals a site-side error even
    on an HTTP 200."""
    code = envelope.get("code")
    if code not in (0, None):
        raise ValueError(f"kaloricketabulky.sk returned error code={code}: {envelope.get('message')}")
    return (envelope.get("data") or {}).get("times") or []


def _parse_diary(payload: list[dict]) -> dict:
    """Parses the diary endpoint's response (a fixed list of meal slots - raňajky,
    desiata, obed, ...) into one health_records-ready dict: day totals plus a
    per-meal/per-food breakdown for /daily_report to point at specific items."""
    meals = []
    for meal in payload:
        items = [_parse_item(f) for f in (meal.get("foodstuff") or [])]
        if not items:
            continue
        meal_kcal = _parse_energy(meal.get("energyTotal"))
        if meal_kcal is None:
            meal_kcal = sum(i.get("kcal") or 0 for i in items)
        meals.append({"nazov": meal.get("title"), "kcal": round(meal_kcal, 1), "polozky": items})

    def _day_total(key: str) -> float:
        return round(sum(i.get(key) or 0 for m in meals for i in m["polozky"]), 2)

    return {
        "energy_kcal": _day_total("kcal"),
        "protein_g": _day_total("protein_g"),
        "carbs_g": _day_total("carbs_g"),
        "fat_g": _day_total("fat_g"),
        "fiber_g": _day_total("fiber_g"),
        "sugar_g": _day_total("sugar_g"),
        "jedla": meals,
    }


async def sync_range(user_id: int, start: date, end: date) -> dict[date, dict | None]:
    """Fetches every day in [start, end] inclusive over one shared cookie-authenticated
    session (mirrors garmin_sync.sync_range's single-login-per-range approach), then
    persists whatever cookies the site sent back - sites commonly rotate/extend session
    cookies on use, so this keeps the stored session fresh for longer.

    Returns {day: data} for days stored successfully, {day: None} for days that failed
    to fetch or store. Raises NutritionAuthRequired if there's no linked account or the
    stored session is rejected, ValueError if the range exceeds MAX_RANGE_DAYS.
    """
    if start > end:
        start, end = end, start

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if len(days) > MAX_RANGE_DAYS:
        raise ValueError(f"Range spans {len(days)} days, max is {MAX_RANGE_DAYS}.")

    cookies = await database.get_nutrition_cookies(user_id)
    if cookies is None:
        raise NutritionAuthRequired(
            "No kaloricketabulky.sk account is linked for this user. Link it via /nutrition_link."
        )

    result: dict[date, dict | None] = {}
    async with aiohttp.ClientSession(cookies=cookies, timeout=_TIMEOUT) as session:
        for day in days:
            url = _DIARY_URL.format(date=day.strftime("%d.%m.%Y"))
            try:
                async with session.get(url, params={"format": "json"}) as resp:
                    if resp.status in (401, 403):
                        raise NutritionAuthRequired(
                            "The stored kaloricketabulky.sk session was rejected. "
                            "Please link it again via /nutrition_link."
                        )
                    resp.raise_for_status()
                    envelope = await resp.json(content_type=None)
                data = _parse_diary(_unwrap_response(envelope))
            except NutritionAuthRequired:
                raise
            except Exception:
                logger.exception(
                    "Failed to fetch nutrition diary for %s (user_id=%s)", day, user_id
                )
                result[day] = None
                continue

            try:
                await database.replace_health_record_for_day(user_id, RECORD_TYPE, day, data)
                result[day] = data
            except Exception:
                logger.exception(
                    "Failed to store nutrition data for %s (user_id=%s)", day, user_id
                )
                result[day] = None

        refreshed_cookies = {c.key: c.value for c in session.cookie_jar}

    if refreshed_cookies:
        await database.save_nutrition_cookies(user_id, refreshed_cookies)

    ok_count = sum(1 for v in result.values() if v is not None)
    logger.info(
        "Nutrition sync for user_id=%s: %d/%d days ok (%s to %s)",
        user_id,
        ok_count,
        len(days),
        start,
        end,
    )
    return result


async def sync_day(user_id: int, day: date) -> dict:
    result = await sync_range(user_id, day, day)
    data = result[day]
    if data is None:
        raise RuntimeError(f"Failed to sync nutrition data for {day.isoformat()}")
    return data


async def sync_catch_up(user_id: int) -> list[date]:
    """Syncs every day from the last synced date (exclusive) up to yesterday, capped
    at MAX_RANGE_DAYS per call. If nothing has ever been synced, backfills the last
    INITIAL_BACKFILL_DAYS days (shorter than Garmin's 30 - diet logging tends to start
    when the user starts using the diary, not months of retroactive history)."""
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    last_synced = await database.get_last_synced_date(user_id, RECORD_TYPE)

    if last_synced is None:
        start = yesterday - timedelta(days=INITIAL_BACKFILL_DAYS - 1)
    else:
        start = last_synced + timedelta(days=1)

    if start > yesterday:
        return []

    end = min(yesterday, start + timedelta(days=MAX_RANGE_DAYS - 1))
    result = await sync_range(user_id, start, end)
    return [d for d, v in result.items() if v is not None]
