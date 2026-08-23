"""Tests for Narwal action buttons."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.button import (  # noqa: E402
    CONSUMABLE_INFO_RESET_DESCRIPTIONS,
    ROBOT_BUTTON_DESCRIPTIONS,
    NarwalConsumableInfoResetButton,
    NarwalRobotActionButton,
    async_setup_entry,
)
from custom_components.narwal.coordinator import CleanSettings  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MapData,
    RoomCleanSettings,
    RoomInfo,
    WorkingStatus,
)

_ROBOT_DESCS = {d.key: d for d in ROBOT_BUTTON_DESCRIPTIONS}


def _coordinator(
    *,
    is_docked: bool,
    dock_state_unknown: bool = False,
    is_station_active: bool = False,
    maintain_items: tuple[int, ...] = (),
    replace_items: tuple[int, ...] = (),
    consumable_info_available: bool = True,
) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1"}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    coord.clean_settings = CleanSettings()
    coord.last_update_success = True
    working_status = (
        WorkingStatus.DOCKED
        if is_docked
        else WorkingStatus.UNKNOWN if dock_state_unknown else WorkingStatus.STANDBY
    )
    state_attrs = dict(
        working_status=working_status,
        is_docked=is_docked,
        dock_state_unknown=dock_state_unknown,
        is_station_active=is_station_active,
        has_recent_active_working_status=False,
        is_returning=False,
        is_charging_to_resume=False,
        is_paused=False,
        is_cleaning=False,
        map_data=None,
        maintain_items=list(maintain_items),
        replace_items=list(replace_items),
        consumable_info_available=consumable_info_available,
    )
    coord.data = MagicMock(**state_attrs)
    coord.client.state = MagicMock(**state_attrs)
    coord.client.state.firmware_version = "1.0.0"
    coord.cloud_consumables = {}
    coord.room_clean_settings_for_rooms = MagicMock(
        side_effect=lambda room_ids: {
            room_id: RoomCleanSettings() for room_id in room_ids
        }
    )
    return coord


def test_robot_action_buttons_follow_cleaning_phase() -> None:
    docked = _coordinator(is_docked=True)
    assert NarwalRobotActionButton(
        docked, _ROBOT_DESCS["start_cleaning"]
    ).available
    assert not NarwalRobotActionButton(
        docked, _ROBOT_DESCS["pause_cleaning"]
    ).available

    active = _coordinator(is_docked=False)
    active.data.working_status = WorkingStatus.CLEANING
    active.data.is_cleaning = True
    active.data.has_recent_active_working_status = True
    active.data.last_active_working_status_time = time.monotonic()

    assert not NarwalRobotActionButton(
        active, _ROBOT_DESCS["start_cleaning"]
    ).available
    assert NarwalRobotActionButton(
        active, _ROBOT_DESCS["pause_cleaning"]
    ).available
    assert NarwalRobotActionButton(
        active, _ROBOT_DESCS["stop_cleaning"]
    ).available


def test_robot_action_buttons_allow_stopping_remapping() -> None:
    remapping = _coordinator(is_docked=False)
    remapping.data.working_status = WorkingStatus.REMAPPING

    assert not NarwalRobotActionButton(
        remapping, _ROBOT_DESCS["start_cleaning"]
    ).available
    assert NarwalRobotActionButton(
        remapping, _ROBOT_DESCS["pause_cleaning"]
    ).available
    assert NarwalRobotActionButton(
        remapping, _ROBOT_DESCS["stop_cleaning"]
    ).available


def test_start_cleaning_button_available_for_unknown_state_wake() -> None:
    """A sleeping robot with unknown status can still be woken by Start Cleaning."""
    coord = _coordinator(is_docked=False, dock_state_unknown=True)

    assert NarwalRobotActionButton(
        coord, _ROBOT_DESCS["start_cleaning"]
    ).available


@pytest.mark.asyncio
async def test_start_cleaning_button_passes_room_profiles() -> None:
    """The start button should use room profiles for whole-home starts."""
    coord = _coordinator(is_docked=True)
    coord.data.map_data = MapData(rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)])
    profile = RoomCleanSettings(fan=FanLevel.MUTE)
    coord.room_clean_settings_for_rooms = MagicMock(
        return_value={4: profile, 7: RoomCleanSettings()}
    )
    coord.client.robot_awake = True
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coord.set_active_room_ids = MagicMock()
    coord.async_set_updated_data = MagicMock()

    await NarwalRobotActionButton(
        coord, _ROBOT_DESCS["start_cleaning"]
    ).async_press()

    kwargs = coord.client.start_rooms.await_args.kwargs
    assert kwargs["room_settings"] == {4: profile, 7: RoomCleanSettings()}
    coord.set_active_room_ids.assert_called_once_with([4, 7])


@pytest.mark.asyncio
async def test_robot_action_revalidates_after_wake_refresh() -> None:
    """Wake refreshes can make a previously available robot command invalid."""
    coord = _coordinator(is_docked=True)
    coord.client.robot_awake = False
    coord.client.wake = AsyncMock()
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    coord.client.state.is_docked = False
    coord.client.state.is_cleaning = True
    coord.client.state.working_status = WorkingStatus.CLEANING

    with pytest.raises(Exception, match="not available"):
        await NarwalRobotActionButton(
            coord, _ROBOT_DESCS["start_cleaning"]
        ).async_press()

    coord.client.wake.assert_awaited_once_with(timeout=10.0)
    coord.client.start_rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_cleaning_button_wakes_unknown_before_starting() -> None:
    """UNKNOWN is only a pre-wake availability state, not an execution state."""
    coord = _coordinator(is_docked=False, dock_state_unknown=True)
    coord.data.map_data = None
    coord.client.robot_awake = False
    coord.client.get_map = AsyncMock()
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coord.async_set_updated_data = MagicMock()

    async def wake_robot(*, timeout: float) -> bool:
        coord.client.state.working_status = WorkingStatus.DOCKED
        coord.client.state.is_docked = True
        coord.client.state.dock_state_unknown = False
        return True

    coord.client.wake = AsyncMock(side_effect=wake_robot)

    await NarwalRobotActionButton(
        coord, _ROBOT_DESCS["start_cleaning"]
    ).async_press()

    coord.client.wake.assert_awaited_once_with(timeout=10.0)
    coord.client.start_rooms.assert_awaited_once_with([])


@pytest.mark.asyncio
async def test_start_cleaning_button_rejects_unknown_after_wake() -> None:
    """A failed wake refresh must not fall through to a start command."""
    coord = _coordinator(is_docked=False, dock_state_unknown=True)
    coord.client.robot_awake = False
    coord.client.wake = AsyncMock(return_value=False)
    coord.client.get_map = AsyncMock()
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    with pytest.raises(Exception, match="not available"):
        await NarwalRobotActionButton(
            coord, _ROBOT_DESCS["start_cleaning"]
        ).async_press()

    coord.client.wake.assert_awaited_once_with(timeout=10.0)
    coord.client.start_rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_cleaning_button_revalidates_after_map_resolution() -> None:
    """Map fetches can make a previously available start command invalid."""
    coord = _coordinator(is_docked=True)
    coord.data.map_data = None
    coord.client.robot_awake = True
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    async def refresh_map() -> None:
        coord.data.map_data = MapData(rooms=[RoomInfo(room_id=4)])
        coord.client.state.is_docked = False
        coord.client.state.is_cleaning = True
        coord.client.state.working_status = WorkingStatus.CLEANING

    coord.client.get_map = AsyncMock(side_effect=refresh_map)

    with pytest.raises(Exception, match="not available"):
        await NarwalRobotActionButton(
            coord, _ROBOT_DESCS["start_cleaning"]
        ).async_press()

    coord.client.start_rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_cleaning_button_revalidates_after_client_map_resolution() -> None:
    """Client-side map fetches can also make a start command invalid."""
    coord = _coordinator(is_docked=True)
    coord.data.map_data = None
    coord.client.state.map_data = None
    coord.client.robot_awake = True
    coord.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    async def refresh_map() -> None:
        coord.client.state.map_data = MapData(rooms=[RoomInfo(room_id=4)])
        coord.client.state.is_docked = False
        coord.client.state.is_cleaning = True
        coord.client.state.working_status = WorkingStatus.CLEANING

    coord.client.get_map = AsyncMock(side_effect=refresh_map)

    with pytest.raises(Exception, match="not available"):
        await NarwalRobotActionButton(
            coord, _ROBOT_DESCS["start_cleaning"]
        ).async_press()

    coord.client.start_rooms.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_consumable_info_reset_button_accepts_stale_refresh_after_clear() -> None:
    coord = _coordinator(is_docked=True, maintain_items=(4,))
    coord.client.robot_awake = True
    coord.client.state.maintain_items = [4]
    coord.client.state.replace_items = []
    coord.client.state.consumable_info_available = False
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

    await button.async_press()

    coord.async_set_updated_data.assert_called_once_with(coord.client.state)


@pytest.mark.asyncio
async def test_setup_adds_consumable_info_reset_only_after_matching_alert() -> None:
    coord = _coordinator(is_docked=True, maintain_items=(4,))
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    entry = MagicMock()
    entry.runtime_data = coord
    added_entities = []

    def add_entities(entities) -> None:
        added_entities.extend(list(entities))

    await async_setup_entry(MagicMock(), entry, add_entities)

    reset_buttons = [
        entity
        for entity in added_entities
        if isinstance(entity, NarwalConsumableInfoResetButton)
    ]
    assert [button.description.key for button in reset_buttons] == [
        "maintenance_wash_ribs_clear"
    ]


def test_consumable_info_reset_button_unavailable_when_item_not_active() -> None:
    coord = _coordinator(is_docked=True)
    description = next(
        desc
        for desc in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if desc.key == "maintenance_wash_ribs_clear"
    )
    button = NarwalConsumableInfoResetButton(coord, description)

    assert not button.available


def test_consumable_info_reset_button_unavailable_when_alert_data_is_stale() -> None:
    coord = _coordinator(
        is_docked=True,
        maintain_items=(4,),
        consumable_info_available=False,
    )
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
