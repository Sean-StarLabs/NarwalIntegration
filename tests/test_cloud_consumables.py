"""Tests for Narwal cloud consumable parsing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.cloud import (  # noqa: E402
    NarwalCloudConsumable,
    NarwalCloudError,
    _is_token_error,
    _raise_for_cloud_error,
)
from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.const import narwal_cloud_hosts  # noqa: E402


async def test_failed_cloud_refresh_is_throttled() -> None:
    """A failed cloud refresh waits for the normal polling interval."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._cloud_client = MagicMock()
    coordinator._cloud_client.async_get_consumables = AsyncMock(
        side_effect=NarwalCloudError("unavailable")
    )
    coordinator._cloud_consumables_lock = asyncio.Lock()
    coordinator._cloud_consumables_last_update = 0.0
    coordinator.cloud_consumables_error = None
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {
        "device_id": "test-device",
        "product_key": "test-product",
    }

    await coordinator.async_refresh_cloud_consumables()

    assert coordinator._cloud_consumables_last_update > 0
    assert coordinator._cloud_consumables_due is False


async def test_cloud_refresh_loop_runs_without_local_polling(monkeypatch) -> None:
    """Cloud refresh scheduling does not depend on local status polls."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.async_refresh_cloud_consumables = AsyncMock()
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await coordinator._cloud_consumables_loop()

    coordinator.async_refresh_cloud_consumables.assert_awaited_once_with()


async def test_cloud_refresh_does_not_change_local_availability() -> None:
    """A cloud-only update must not mark an unreachable vacuum available."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "filter",
            "name": "Filter",
            "usage_duration": 3600,
            "total_duration": 7200,
            "progress_bar_switch": 1,
        }
    )
    coordinator._cloud_client = MagicMock()
    coordinator._cloud_client.async_get_consumables = AsyncMock(
        return_value=[consumable]
    )
    coordinator._cloud_consumables_lock = asyncio.Lock()
    coordinator._cloud_consumables_last_update = 0.0
    coordinator.cloud_consumables = {}
    coordinator.cloud_consumables_error = None
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {
        "device_id": "test-device",
        "product_key": "test-product",
    }
    coordinator.async_set_updated_data = MagicMock()
    coordinator.async_update_listeners = MagicMock()

    await coordinator.async_refresh_cloud_consumables()

    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.async_set_updated_data.assert_not_called()


def test_cloud_consumable_life_values() -> None:
    """Cloud consumables expose used and remaining lifetime values."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "NE8GfnsbR9",
            "name": "Base Station Replaceable Cleaning Filter",
            "usage_duration": 71078,
            "total_duration": 54000,
            "progress_bar_switch": 1,
            "reset_btn_switch": 1,
            "subtitle": "Clean regularly",
        }
    )

    assert consumable.has_life_counter is True
    assert consumable.used_hours == 19.7
    assert consumable.total_hours == 15.0
    assert consumable.remaining_hours == 0.0
    assert consumable.used_percent == 131.6
    assert consumable.remaining_percent == 0.0
    assert consumable.is_overdue is True
    assert consumable.reset_supported is True


def test_cloud_consumable_without_counter_is_ignored() -> None:
    """Cloud items without a progress counter are not life sensors."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "KBpp7EeVIt",
            "name": "Mop Self-Cleaning Scraper",
            "usage_duration": 0,
            "total_duration": 0,
            "progress_bar_switch": 0,
        }
    )

    assert consumable.has_life_counter is False
    assert consumable.is_overdue is False


def test_token_errors_are_detected() -> None:
    """Narwal access token errors trigger re-login."""
    assert _is_token_error({"err_code": 130105}) is True
    assert _is_token_error({"err_code": 130109}) is True
    assert _is_token_error({"msg": "access token error"}) is True
    assert _is_token_error({"code": 0, "msg": "succeed!"}) is False


def test_err_code_zero_is_cloud_success() -> None:
    """Narwal endpoints may report successful requests through err_code."""
    _raise_for_cloud_error({"err_code": 0, "result": []}, "test")


def test_cloud_hosts_follow_selected_region() -> None:
    assert narwal_cloud_hosts("us") == (
        "https://us-idass.narwaltech.com",
        "https://us-app.narwaltech.com",
    )

    with pytest.raises(ValueError, match="Unsupported Narwal cloud region"):
        narwal_cloud_hosts("invalid")
