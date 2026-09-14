"""Generic orchestration for keeping every linked external integration's data fresh
for a given user before something (like /daily_report) reads it - one place that
knows about every integration (Garmin, kaloricketabulky.sk nutrition, and whatever
comes next) instead of each caller hardcoding calls to each service's own sync
module. Add a new integration by appending one _Integration entry to _INTEGRATIONS
below; nothing else needs to change.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from app import database
from app.garmin import sync as garmin_sync
from app.nutrition import sync as nutrition_sync

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Integration:
    service: str  # matches the `service` column in the integrations table
    sync_day: Callable[[int, date], Awaitable[dict]]
    sync_catch_up: Callable[[int], Awaitable[list[date]]]
    auth_required: type[Exception]


_INTEGRATIONS = [
    _Integration(
        "garmin", garmin_sync.sync_day, garmin_sync.sync_catch_up, garmin_sync.GarminAuthRequired
    ),
    _Integration(
        "nutrition",
        nutrition_sync.sync_day,
        nutrition_sync.sync_catch_up,
        nutrition_sync.NutritionAuthRequired,
    ),
]


async def ensure_day_synced(user_id: int, day: date, force: bool = False) -> None:
    """For every integration this user has linked, makes sure `day`'s data is in
    health_records. An integration the user hasn't linked is skipped entirely, and
    one integration failing never blocks the others.

    force=False (the default - used by a manually-triggered /daily_report): calls
    each integration's own sync_catch_up, which already skips re-fetching days it has
    already synced, so this is cheap to call even when nothing needs to happen.
    force=True (meant for the future scheduled morning report - see README): always
    re-fetches `day` directly via sync_day, overwriting whatever's already stored -
    covers data that changed after an earlier sync (e.g. a late-night workout logged
    after the fact, or an edited food diary entry).
    """
    for integration in _INTEGRATIONS:
        creds = await database.get_integration_credentials(user_id, integration.service)
        if creds is None:
            continue
        try:
            if force:
                await integration.sync_day(user_id, day)
            else:
                await integration.sync_catch_up(user_id)
        except integration.auth_required as e:
            logger.warning("%s sync skipped for user_id=%s: %s", integration.service, user_id, e)
        except Exception:
            logger.exception("%s sync failed for user_id=%s", integration.service, user_id)
