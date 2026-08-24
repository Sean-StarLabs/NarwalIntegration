"""Tests for Narwal switch entities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

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
    coordinator.async_refresh_dock_status = AsyncMock()
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
    coordinator.async_refresh_dock_status.assert_awaited_once_with()
    coordinator.async_set_updated_data.assert_not_called()


@pytest.mark.asyncio
async def test_dock_task_switch_stops_active_task() -> None:
    coordinator = _coordinator(_state(station_activity=1))
    coordinator.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.client.cancel = AsyncMock()
    coordinator.client.stop = AsyncMock()
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])

    await switch.async_turn_off()

    coordinator.client.stop_dock_task.assert_awaited_once_with("emptying_dustbin")
    coordinator.client.cancel.assert_not_called()
    coordinator.client.stop.assert_not_called()
    coordinator.async_refresh_dock_status.assert_awaited_once_with()


def test_dock_task_switch_exposes_drying_progress_attributes() -> None:
    state = _state()
    state.dock_activity = 4
    state.dry_mop_remaining_time = 600
    state.mop_drying_elapsed = 120
    state.mop_drying_target = 720
    coordinator = _coordinator(state)
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])

    assert switch.is_on is True
    assert switch.extra_state_attributes == {
        "time_left": "10m",
        "progress": 17,
    }


def test_dock_task_switch_exposes_app_started_dock_bag_drying() -> None:
    state = _state()
    state.update_from_working_status({"12": 9_000, "13": 18_000, "19": {}})
    coordinator = _coordinator(state)
    switch = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"])

    assert switch.is_on is True
    assert switch.available
    assert switch.extra_state_attributes == {
        "time_left": "2h 30m",
        "progress": 50,
    }


def test_working_status_field_10_11_reports_dust_bin_drying() -> None:
    state = _state()
    state.update_from_working_status({"10": 9_000, "11": 18_000, "19": {}})
    coordinator = _coordinator(state)

    assert NarwalDockTaskSwitch(coordinator, _DESCS["dry_dust_bin"]).is_on is True
    assert NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"]).is_on is False


def test_dock_task_switch_allows_verified_parallel_dry_start() -> None:
    state = _state()
    state.update_from_working_status({"8": 9_000, "9": 18_000, "19": {}})
    coordinator = _coordinator(state)

    dry_mop = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])
    dry_dust_bin = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dust_bin"])
    dry_dock_bag = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"])
    empty = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])
    wash = NarwalDockTaskSwitch(coordinator, _DESCS["wash_mop"])

    assert dry_mop.available
    assert dry_dust_bin.is_on is False
    assert dry_dust_bin.available
    assert dry_dock_bag.is_on is False
    assert not dry_dock_bag.available
    assert not empty.available
    assert not wash.available


def test_dock_task_switch_allows_verified_parallel_dry_start_reverse() -> None:
    state = _state()
    state.update_from_working_status({"10": 9_000, "11": 18_000, "19": {}})
    coordinator = _coordinator(state)

    dry_mop = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])
    dry_dock_bag = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"])
    empty = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])
    wash = NarwalDockTaskSwitch(coordinator, _DESCS["wash_mop"])

    assert dry_mop.is_on is False
    assert dry_mop.available
    assert not dry_dock_bag.available
    assert not empty.available
    assert not wash.available


def test_dock_task_switch_disables_unscoped_parallel_stops() -> None:
    state = _state()
    state.update_from_working_status(
        {"8": 9_000, "9": 18_000, "10": 9_000, "11": 18_000, "19": {}}
    )
    coordinator = _coordinator(state)

    dry_mop = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])
    dry_dust_bin = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dust_bin"])

    assert dry_mop.is_on is True
    assert dry_dust_bin.is_on is True
    assert not dry_mop.available
    assert not dry_dust_bin.available


def test_dock_task_switch_blocks_unverified_parallel_starts() -> None:
    coordinator = _coordinator(_state(station_activity=1))

    empty = NarwalDockTaskSwitch(coordinator, _DESCS["empty_dustbin"])
    wash = NarwalDockTaskSwitch(coordinator, _DESCS["wash_mop"])
    dry_mop = NarwalDockTaskSwitch(coordinator, _DESCS["dry_mop"])
    dry_dust_bin = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dust_bin"])
    dry_dock_bag = NarwalDockTaskSwitch(coordinator, _DESCS["dry_dock_bag"])

    assert empty.is_on is True
    assert empty.available
    assert wash.is_on is False
    assert not wash.available
    assert dry_mop.is_on is False
    assert not dry_mop.available
    assert not dry_dust_bin.available
    assert not dry_dock_bag.available
