from datetime import date
from unittest.mock import AsyncMock

import pytest

from app import integrations
from app.integrations import _Integration, ensure_day_synced

_DAY = date(2026, 9, 14)


class _FakeAuthRequired(Exception):
    pass


def _fake_integration(sync_day=None, sync_catch_up=None):
    return _Integration(
        service="fake",
        sync_day=sync_day or AsyncMock(),
        sync_catch_up=sync_catch_up or AsyncMock(),
        auth_required=_FakeAuthRequired,
    )


@pytest.fixture
def patch_credentials(monkeypatch):
    def _patch(value):
        monkeypatch.setattr(
            integrations.database, "get_integration_credentials", AsyncMock(return_value=value)
        )

    return _patch


async def test_skips_unlinked_integration(monkeypatch, patch_credentials):
    patch_credentials(None)
    fake = _fake_integration()
    monkeypatch.setattr(integrations, "_INTEGRATIONS", [fake])

    await ensure_day_synced(1, _DAY)

    fake.sync_day.assert_not_called()
    fake.sync_catch_up.assert_not_called()


async def test_not_forced_calls_catch_up(monkeypatch, patch_credentials):
    patch_credentials({"token": "x"})
    fake = _fake_integration()
    monkeypatch.setattr(integrations, "_INTEGRATIONS", [fake])

    await ensure_day_synced(1, _DAY, force=False)

    fake.sync_catch_up.assert_awaited_once_with(1)
    fake.sync_day.assert_not_called()


async def test_forced_calls_sync_day(monkeypatch, patch_credentials):
    patch_credentials({"token": "x"})
    fake = _fake_integration()
    monkeypatch.setattr(integrations, "_INTEGRATIONS", [fake])

    await ensure_day_synced(1, _DAY, force=True)

    fake.sync_day.assert_awaited_once_with(1, _DAY)
    fake.sync_catch_up.assert_not_called()


async def test_auth_required_is_swallowed(monkeypatch, patch_credentials):
    patch_credentials({"token": "x"})
    fake = _fake_integration(sync_catch_up=AsyncMock(side_effect=_FakeAuthRequired("no session")))
    monkeypatch.setattr(integrations, "_INTEGRATIONS", [fake])

    await ensure_day_synced(1, _DAY)  # must not raise


async def test_one_integration_failing_does_not_block_another(monkeypatch, patch_credentials):
    patch_credentials({"token": "x"})
    failing = _fake_integration(sync_catch_up=AsyncMock(side_effect=RuntimeError("boom")))
    failing = _Integration(
        service="failing",
        sync_day=failing.sync_day,
        sync_catch_up=failing.sync_catch_up,
        auth_required=_FakeAuthRequired,
    )
    healthy = _fake_integration()
    healthy = _Integration(
        service="healthy",
        sync_day=healthy.sync_day,
        sync_catch_up=healthy.sync_catch_up,
        auth_required=_FakeAuthRequired,
    )
    monkeypatch.setattr(integrations, "_INTEGRATIONS", [failing, healthy])

    await ensure_day_synced(1, _DAY)  # must not raise

    healthy.sync_catch_up.assert_awaited_once_with(1)
