"""Tests for Narwal action buttons."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.button import (  # noqa: E402
    BUTTON_DESCRIPTIONS,
    NarwalActionButton,
)
from custom_components.narwal.narwal_client import CommandResponse, CommandResult  # noqa: E402


_DESCS = {d.key: d for d in BUTTON_DESCRIPTIONS}


def _coordinator(
    *,
    is_docked: bool,
    is_station_active: bool = False,
    is_cleaning: bool = False,
) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1"}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    state_attrs = dict(
        is_docked=is_docked,
        is_station_active=is_station_active,
        is_cleaning=is_cleaning,
        has_recent_active_working_status=is_cleaning,
        is_returning=False,
        is_charging_to_resume=False,
    )
    coord.client.state = MagicMock(**state_attrs)
    coord.client.state.firmware_version = "1.0.0"
    coord.last_update_success = True
    coord.data = MagicMock(**state_attrs)
    return coord


def test_station_button_unavailable_away_from_dock() -> None:
    coord = _coordinator(is_docked=False)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert not button.available


def test_station_button_available_when_docked_and_idle() -> None:
    coord = _coordinator(is_docked=True)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert button.available


def test_station_button_unavailable_when_station_active() -> None:
    coord = _coordinator(is_docked=True, is_station_active=True)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert not button.available


@pytest.mark.asyncio
async def test_station_button_rejects_unavailable_press() -> None:
    coord = _coordinator(is_docked=False)
    coord.client.robot_awake = True
    coord.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    with pytest.raises(Exception, match="cannot be started"):
        await NarwalActionButton(coord, _DESCS["empty_dustbin"]).async_press()

    coord.client.empty_dustbin.assert_not_awaited()


@pytest.mark.asyncio
async def test_station_button_revalidates_after_wake_refresh() -> None:
    coord = _coordinator(is_docked=True)
    coord.client.robot_awake = False
    coord.client.wake = AsyncMock()
    coord.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coord.client.state.is_docked = False

    with pytest.raises(Exception, match="cannot be started"):
        await NarwalActionButton(coord, _DESCS["empty_dustbin"]).async_press()

    coord.client.wake.assert_awaited_once_with(timeout=10.0)
    coord.client.empty_dustbin.assert_not_awaited()
