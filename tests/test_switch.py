"""Tests for Narwal switch entities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    NarwalState,
    WorkingStatus,
)
from custom_components.narwal.switch import (  # noqa: E402
    DOCK_TASK_SWITCHES,
    NarwalDockTaskSwitch,
)

_DESCS = {description.key: description for description in DOCK_TASK_SWITCHES}


def _state(*, docked: bool = True, station_activity: int = 0) -> NarwalState:
    state = NarwalState(
        working_status=WorkingStatus.DOCKED if docked else WorkingStatus.STANDBY
    )
    state.firmware_version = "1.0.0"
    state.station_activity = station_activity
    if docked:
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.raw_base_status = {"11": 3, "47": 1}
    else:
        state.dock_field11 = 1
        state.dock_field47 = 2
        state.raw_base_status = {"11": 1, "47": 2}
    return state


def _coordinator(state: NarwalState) -> SimpleNamespace:
    coordinator = SimpleNamespace()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.robot_awake = True
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.local_available = True
    coordinator._active_dock_task_key = None
    coordinator.set_active_dock_task_key = (
        lambda task_key: NarwalCoordinator.set_active_dock_task_key(
            coordinator, task_key
        )
    )
    coordinator.current_dock_task_key = (
        lambda fresh_state=None: NarwalCoordinator.current_dock_task_key(
            coordinator, fresh_state
        )
    )
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def test_dock_task_switch_unavailable_away_from_dock() -> None:
    switch = NarwalDockTaskSwitch(
        _coordinator(_state(docked=False)),
        _DESCS["empty_dustbin"],
    )

    assert not switch.available


def test_dock_task_switch_available_when_docked_and_idle() -> None:
    switch = NarwalDockTaskSwitch(
        _coordinator(_state()),
        _DESCS["empty_dustbin"],
    )

    assert switch.available
    assert switch.is_on is False


def test_dock_task_switch_active_for_matching_station_task() -> None:
    coordinator = _coordinator(_state(station_activity=1))
    NarwalCoordinator._sync_active_dock_task_key(coordinator, coordinator.data)
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])

    assert switch.is_on is True
    assert switch.available


def test_dock_task_switch_unavailable_when_station_activity_is_stale_off_dock() -> None:
    state = _state(docked=False, station_activity=1)
    switch = NarwalDockTaskSwitch(_coordinator(state), _DESCS["empty_dustbin"])

    assert switch.is_on is False
    assert not switch.available


@pytest.mark.asyncio
async def test_dock_task_switch_rejects_unavailable_start() -> None:
    coordinator = _coordinator(_state(docked=False))
    coordinator.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])

    with pytest.raises(Exception, match="cannot be started"):
        await switch.async_turn_on()

    coordinator.client.empty_dustbin.assert_not_awaited()


@pytest.mark.asyncio
async def test_dock_task_switch_starts_task() -> None:
    coordinator = _coordinator(_state())
    coordinator.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])

    await switch.async_turn_on()

    coordinator.client.empty_dustbin.assert_awaited_once_with()
    assert coordinator._active_dock_task_key == "empty_dustbin"
    coordinator.async_set_updated_data.assert_called_once_with(coordinator.client.state)


@pytest.mark.asyncio
async def test_dock_task_switch_stops_active_task() -> None:
    coordinator = _coordinator(_state(station_activity=1))
    NarwalCoordinator._sync_active_dock_task_key(coordinator, coordinator.data)
    coordinator.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.client.cancel = AsyncMock()
    coordinator.client.stop = AsyncMock()
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])

    await switch.async_turn_off()

    coordinator.client.stop_dock_task.assert_awaited_once_with()
    coordinator.client.cancel.assert_not_called()
    coordinator.client.stop.assert_not_called()
    assert coordinator._active_dock_task_key is None


def test_dock_task_switch_exposes_drying_progress_attributes() -> None:
    state = _state()
    state.dock_activity = 4
    state.dry_mop_remaining_time = 600
    state.mop_drying_elapsed = 120
    state.mop_drying_target = 720
    coordinator = _coordinator(state)
    NarwalCoordinator._sync_active_dock_task_key(coordinator, coordinator.data)
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])

    assert switch.is_on is True
    assert switch.extra_state_attributes == {
        "dock_active": True,
        "docked": True,
        "active": True,
        "raw_task": "drying_mop",
        "task": "Drying mop",
        "time_left": "10m",
        "time_left_minutes": 10,
        "elapsed_minutes": 2,
        "target_minutes": 12,
        "progress": 17,
    }


def test_dock_task_switch_exposes_app_started_dock_bag_drying() -> None:
    state = _state()
    state.update_from_working_status({"12": 9_000, "13": 18_000, "19": {}})
    coordinator = _coordinator(state)
    NarwalCoordinator._sync_active_dock_task_key(coordinator, coordinator.data)
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"])

    assert switch.is_on is True
    assert switch.available
    assert switch.extra_state_attributes == {
        "dock_active": True,
        "docked": True,
        "active": True,
        "raw_task": "dry_dock_bag",
        "task": "Drying / disinfecting dock bag",
        "time_left": "2h 30m",
        "time_left_minutes": 150,
        "timer_fields": "12/13",
        "elapsed_minutes": 150,
        "target_minutes": 300,
        "progress": 50,
    }
