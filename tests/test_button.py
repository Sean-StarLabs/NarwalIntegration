"""Tests for Narwal action buttons."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.button import (  # noqa: E402
    BUTTON_DESCRIPTIONS,
    CONSUMABLE_INFO_RESET_DESCRIPTIONS,
    NarwalActionButton,
    NarwalConsumableInfoResetButton,
)
from custom_components.narwal.narwal_client import CommandResponse, CommandResult  # noqa: E402


_DESCS = {d.key: d for d in BUTTON_DESCRIPTIONS}


def _coordinator(
    *,
    is_docked: bool,
    dock_state_unknown: bool = False,
    is_station_active: bool = False,
    maintain_items: tuple[int, ...] = (),
    replace_items: tuple[int, ...] = (),
) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1"}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    coord.client.state = MagicMock()
    coord.client.state.firmware_version = "1.0.0"
    coord.last_update_success = True
    coord.data = MagicMock(
        is_docked=is_docked,
        dock_state_unknown=dock_state_unknown,
        is_station_active=is_station_active,
        maintain_items=list(maintain_items),
        replace_items=list(replace_items),
    )
    return coord


def test_station_button_unavailable_away_from_dock() -> None:
    coord = _coordinator(is_docked=False)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert not button.available


def test_station_button_available_when_docked_and_idle() -> None:
    coord = _coordinator(is_docked=True)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert button.available


def test_station_button_available_when_dock_state_unknown() -> None:
    coord = _coordinator(is_docked=False, dock_state_unknown=True)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert button.available


def test_stop_dock_task_available_only_during_station_task() -> None:
    idle = _coordinator(is_docked=True)
    active = _coordinator(is_docked=True, is_station_active=True)

    assert not NarwalActionButton(idle, _DESCS["stop_dock_task"]).available
    assert NarwalActionButton(active, _DESCS["stop_dock_task"]).available


@pytest.mark.asyncio
async def test_stop_dock_task_calls_station_stop() -> None:
    coord = _coordinator(is_docked=True, is_station_active=True)
    coord.client.robot_awake = True
    coord.client.state.is_station_active = True
    coord.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coord.client.cancel = AsyncMock()
    coord.client.stop = AsyncMock()
    coord.async_set_updated_data = MagicMock()

    await NarwalActionButton(coord, _DESCS["stop_dock_task"]).async_press()

    coord.client.stop_dock_task.assert_awaited_once_with()
    coord.client.cancel.assert_not_called()
    coord.client.stop.assert_not_called()
    coord.async_set_updated_data.assert_called_once_with(coord.client.state)


@pytest.mark.asyncio
async def test_consumable_info_reset_button_clears_maintenance_item() -> None:
    coord = _coordinator(is_docked=True, maintain_items=(4,))
    coord.client.robot_awake = True
    coord.client.state.maintain_items = []
    coord.client.state.replace_items = []
    coord.client.reset_consumable_info = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coord.async_set_updated_data = MagicMock()

    description = next(
        desc
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if desc.key == "maintenance_wash_ribs_clear"
    )
    button = NarwalConsumableInfoResetButton(coord, description)

    assert button.available

    await button.async_press()

    coord.client.reset_consumable_info.assert_awaited_once_with(
        maintain_items=(4,),
        replace_items=(),
    )
    coord.async_set_updated_data.assert_called_once_with(coord.client.state)


@pytest.mark.asyncio
async def test_consumable_info_reset_button_accepts_applied_result() -> None:
    coord = _coordinator(is_docked=True, maintain_items=(4,))
    coord.client.robot_awake = True
    coord.client.state.maintain_items = []
    coord.client.state.replace_items = []
    coord.client.reset_consumable_info = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.APPLIED)
    )
    coord.async_set_updated_data = MagicMock()

    description = next(
        desc
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if desc.key == "maintenance_wash_ribs_clear"
    )
    button = NarwalConsumableInfoResetButton(coord, description)

    await button.async_press()

    coord.async_set_updated_data.assert_called_once_with(coord.client.state)


def test_consumable_info_reset_button_unavailable_when_item_not_active() -> None:
    coord = _coordinator(is_docked=True)
    description = next(
        desc
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if desc.key == "maintenance_wash_ribs_clear"
    )
    button = NarwalConsumableInfoResetButton(coord, description)

    assert not button.available


@pytest.mark.asyncio
async def test_consumable_info_reset_button_rejects_inactive_item_press() -> None:
    coord = _coordinator(is_docked=True)
    description = next(
        desc
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if desc.key == "maintenance_wash_ribs_clear"
    )
    button = NarwalConsumableInfoResetButton(coord, description)

    with pytest.raises(Exception, match="not active"):
        await button.async_press()


def test_consumable_info_reset_buttons_skip_autoclear_items() -> None:
    """Physical/auto-clearing replacement alerts should not get manual clear buttons."""
    replacement_items = {
        item
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        for item in desc.replace_items
    }

    assert 6 not in replacement_items  # detergent
    assert 23 not in replacement_items  # heavy detergent
