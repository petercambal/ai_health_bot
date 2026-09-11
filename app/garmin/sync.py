import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from garminconnect import Garmin

from app import database

logger = logging.getLogger(__name__)

RECORD_TYPE = "garmin_daily"
INITIAL_BACKFILL_DAYS = 30


class GarminAuthRequired(Exception):
    """No usable Garmin session for this user - they need to (re-)run the bootstrap."""


def _safe(name: str, fetch_fn) -> Any:
    """Isolates one Garmin endpoint's failure so it doesn't take down the whole
    day's digest - Garmin's API is flaky per-endpoint (e.g. no data for a rest day)."""
    try:
        return fetch_fn()
    except Exception as e:  # noqa: BLE001 - routine (e.g. no data for a rest day), not an error
        logger.debug("Garmin endpoint %s failed: %s", name, e)
        return None


def _first(value: Any) -> dict:
    if isinstance(value, list) and value:
        return value[0] if isinstance(value[0], dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def _build_digest(client: Garmin, day: date) -> dict:
    """Pulls the same signals as the original garmin_to_sqlite.py daily_summaries
    table, plus a light activities list - not per-minute intraday series, sleep
    phase intervals, or lap-level splits. Easy to extend if you want those too."""
    date_str = day.isoformat()

    summary = _safe("user_summary", lambda: client.get_user_summary(date_str)) or {}
    sleep_data = _safe("sleep", lambda: client.get_sleep_data(date_str)) or {}
    hrv_data = _safe("hrv", lambda: client.get_hrv_data(date_str)) or {}
    body_battery = _safe("body_battery", lambda: client.get_body_battery(date_str))
    body_composition = _safe("body_composition", lambda: client.get_body_composition(date_str)) or {}
    max_metrics = _safe("max_metrics", lambda: client.get_max_metrics(date_str))
    training_status = _safe("training_status", lambda: client.get_training_status(date_str)) or {}
    training_readiness = _safe(
        "training_readiness", lambda: client.get_training_readiness(date_str)
    )
    hydration_data = _safe("hydration", lambda: client.get_hydration_data(date_str)) or {}
    respiration_data = _safe("respiration", lambda: client.get_respiration_data(date_str)) or {}
    spo2_data = _safe("spo2", lambda: client.get_spo2_data(date_str)) or {}
    activities = (
        _safe("activities", lambda: client.get_activities_by_date(date_str, date_str)) or []
    )

    bb_entry = _first(body_battery)
    tr_readiness = _first(training_readiness)
    hrv_summary = hrv_data.get("hrvSummary", {})
    sleep_dto = sleep_data.get("dailySleepDTO", {})

    tot_avg = body_composition.get("totalAverage", {}) or {}
    raw_w = tot_avg.get("weight")
    weight_kg = round(raw_w / 1000.0, 2) if raw_w and raw_w > 500 else raw_w

    vo2_max = None
    if isinstance(max_metrics, list) and max_metrics:
        vo2_max = (max_metrics[0].get("generic") or {}).get("vo2MaxPreciseValue")

    return {
        "steps_count": summary.get("totalSteps"),
        "steps_goal": summary.get("dailyStepGoal"),
        "distance_km": round(summary["totalDistanceMeters"] / 1000.0, 2)
        if summary.get("totalDistanceMeters")
        else None,
        "active_calories_kcal": summary.get("activeKilocalories"),
        "bmr_calories_kcal": summary.get("bmrKilocalories"),
        "total_calories_kcal": summary.get("totalKilocalories"),
        "floors_climbed": summary.get("floorsClimbed"),
        "high_intensity_minutes": summary.get("vigorousIntensityMinutes"),
        "moderate_intensity_minutes": summary.get("moderateIntensityMinutes"),
        "resting_heart_rate": summary.get("restingHeartRate"),
        "min_heart_rate": summary.get("minHeartRate"),
        "max_heart_rate": summary.get("maxHeartRate"),
        "overall_stress_score": summary.get("averageStressLevel"),
        "body_battery_charged": bb_entry.get("charged") or summary.get("bodyBatteryChargedValue"),
        "body_battery_drained": bb_entry.get("drained") or summary.get("bodyBatteryDrainedValue"),
        "body_battery_max": summary.get("bodyBatteryHighestValue"),
        "body_battery_min": summary.get("bodyBatteryLowestValue"),
        "hrv_weekly_avg": hrv_summary.get("weeklyAvg") or sleep_data.get("avgOvernightHrv"),
        "hrv_status": hrv_summary.get("status") or sleep_data.get("hrvStatus"),
        "avg_spo2": spo2_data.get("averageSpO2"),
        "avg_respiration_waking": respiration_data.get("avgWakingRespirationValue"),
        "avg_respiration_sleep": respiration_data.get("avgSleepRespirationValue"),
        "sleep_score": (sleep_dto.get("sleepScores", {}) or {}).get("overall", {}).get("value"),
        "vo2_max": vo2_max,
        "weight_kg": weight_kg,
        "bmi": tot_avg.get("bmi"),
        "body_fat_pct": tot_avg.get("bodyFat") or tot_avg.get("bodyFatPercentage"),
        "training_readiness_score": tr_readiness.get("score")
        or (tr_readiness.get("inputData") or {}).get("score"),
        "training_status": training_status.get("trainingStatus") or training_status.get("status"),
        "hydration_intake_ml": hydration_data.get("valueInML"),
        "hydration_goal_ml": hydration_data.get("goalInML"),
        "activities": [
            {
                "activity_id": act.get("activityId"),
                "name": act.get("activityName"),
                "type": (act.get("activityType") or {}).get("typeKey"),
                "start_time": act.get("startTimeLocal"),
                "duration_seconds": act.get("duration"),
                "distance_m": act.get("distance"),
                "calories": act.get("calories"),
                "avg_hr": act.get("averageHR"),
                "max_hr": act.get("maxHR"),
                "aerobic_training_effect": act.get("aerobicTrainingEffect"),
                "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
                "training_load": act.get("activityTrainingLoad"),
            }
            for act in activities
            if act.get("activityId")
        ],
    }


def _login_blocking(session_json: str) -> tuple[Garmin, str]:
    """Logs in using ONLY the stored session - never falls back to a password,
    since none is stored. Raises GarminAuthRequired if the session can't be used."""
    client = Garmin()
    try:
        client.login(tokenstore=session_json)
    except Exception as e:
        raise GarminAuthRequired(
            "Uložená Garmin session nie je platná. Treba znova spustiť bootstrap "
            "(`uv run python -m app.garmin.bootstrap <telegram_id>`)."
        ) from e
    return client, client.client.dumps()


async def sync_day(user_id: int, day: date) -> dict:
    session_json = await database.get_garmin_session(user_id)
    if session_json is None:
        raise GarminAuthRequired(
            "Pre tohto používateľa nie je uložený žiadny Garmin účet. Spusti "
            "`uv run python -m app.garmin.bootstrap <telegram_id>`."
        )

    def _blocking() -> tuple[dict, str]:
        client, refreshed_session_json = _login_blocking(session_json)
        digest = _build_digest(client, day)
        return digest, refreshed_session_json

    digest, refreshed_session_json = await asyncio.to_thread(_blocking)

    account_email = await database.get_garmin_email(user_id)
    await database.save_garmin_session(user_id, account_email, refreshed_session_json)
    await database.replace_health_record_for_day(user_id, RECORD_TYPE, day, digest)
    logger.info("Garmin sync complete for user_id=%s day=%s", user_id, day)
    return digest


async def sync_catch_up(user_id: int) -> list[date]:
    """Syncs every day from the last synced date (exclusive) up to yesterday. If
    nothing has ever been synced, backfills the last INITIAL_BACKFILL_DAYS days."""
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    last_synced = await database.get_last_synced_date(user_id, RECORD_TYPE)

    if last_synced is None:
        start = yesterday - timedelta(days=INITIAL_BACKFILL_DAYS - 1)
    else:
        start = last_synced + timedelta(days=1)

    if start > yesterday:
        return []

    days = [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]
    for day in days:
        await sync_day(user_id, day)
        await asyncio.sleep(0.5)  # be gentle with Garmin's API, mirrors the original script
    return days
