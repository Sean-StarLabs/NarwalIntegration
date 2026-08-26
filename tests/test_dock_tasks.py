"""Tests for Narwal dock task entities and command gates."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.dock_tasks import (  # noqa: E402
    can_start_dock_task,
    can_start_robot_clean,
    can_stop_dock_task,
)
from custom_components.narwal.switch import (  # noqa: E402
    DOCK_TASK_SWITCHES,
    NarwalDockTaskSwitch,
)
from narwal_client.const import CommandResult, WorkingStatus  # noqa: E402
from narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    CommandResponse,
    NarwalState,
)


def _docked_state() -> NarwalState:
    """Return an idle on-dock state."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    state.dock_field47 = 3
    return state


def _coordinator(state: NarwalState | None = None) -> MagicMock:
    """Return a minimal coordinator stub for switch entity tests."""
    state = state or _docked_state()
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_device", "model": "flow"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.config_entry.options = {}
    coordinator.client.state = state
    coordinator.client.robot_awake = True
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.has_fresh_state = True
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
    coordinator.dock_action_lock = asyncio.Lock()
    return coordinator


def _switch(task_key: str, state: NarwalState | None = None) -> NarwalDockTaskSwitch:
    """Build one dock task switch by key."""
    descriptions = {description.key: description for description in DOCK_TASK_SWITCHES}
    return NarwalDockTaskSwitch(_coordinator(state), descriptions[task_key])


def test_five_dock_task_switches_are_exposed() -> None:
    """The dock exposes exactly the five app-visible task controls."""
    assert [description.key for description in DOCK_TASK_SWITCHES] == [
        DOCK_TASK_EMPTY_DUSTBIN,
        DOCK_TASK_WASH_MOP,
        DOCK_TASK_DRY_MOP,
        DOCK_TASK_DRY_DUST_BIN,
        DOCK_TASK_DRY_DOCK_BAG,
    ]


def test_dock_task_switch_belongs_to_dock_device() -> None:
    """Dock task controls are grouped under the dock device."""
    switch = _switch(DOCK_TASK_EMPTY_DUSTBIN)

    assert switch._attr_device_info["identifiers"] == {("narwal", "test_device_dock")}
    assert switch._attr_device_info["via_device"] == ("narwal", "test_device")


def test_idle_docked_state_can_start_any_single_task() -> None:
    """An idle dock exposes start controls for all known tasks."""
    state = _docked_state()

    assert can_start_dock_task(state)
    assert all(
        can_start_dock_task(state, description.key)
        for description in DOCK_TASK_SWITCHES
    )


def test_cleaning_state_hides_dock_start_controls() -> None:
    """Robot cleaning context blocks dock starts."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.dock_presence = 2
    state.dock_field11 = 1
    state.dock_field47 = 2

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_task_completed_state_hides_dock_start_controls() -> None:
    """TASK_COMPLETED is still return-to-dock context, not an idle station."""
    state = _docked_state()
    state.working_status = WorkingStatus.TASK_COMPLETED

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_unknown_working_status_hides_dock_start_controls() -> None:
    """Unknown robot status is not treated as safe dock-idle state."""
    state = _docked_state()
    state.working_status = WorkingStatus.UNKNOWN

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_start_robot_clean(state)
    assert not can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_assumed_robot_clean_hides_dock_start_controls() -> None:
    """An accepted robot start blocks dock starts until telemetry catches up."""
    state = _docked_state()
    state.assume_robot_clean()

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_start_robot_clean(state)


def test_active_known_task_is_on_and_stoppable() -> None:
    """Coarse station activity maps to the relevant dock task switch."""
    state = _docked_state()
    state.station_activity = 1

    empty = _switch(DOCK_TASK_EMPTY_DUSTBIN, state)
    wash = _switch(DOCK_TASK_WASH_MOP, state)

    assert empty.is_on
    assert empty.available
    assert not wash.is_on
    assert not wash.available


def test_dock_task_switch_unavailable_when_state_is_stale() -> None:
    """Dock starts fail closed when polling returned cached state."""
    coordinator = _coordinator()
    coordinator.has_fresh_state = False
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    assert not switch.available


async def test_dock_task_switch_restores_on_state_as_private_guard() -> None:
    """A restored on switch blocks conflicting starts until fresh status corrects it."""
    state = _docked_state()
    coordinator = _coordinator(state)
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])
    switch.async_get_last_state = AsyncMock(return_value=MagicMock(state="on"))

    await switch.async_added_to_hass()

    assert state.assumed_active_dock_task == DOCK_TASK_EMPTY_DUSTBIN
    assert not can_start_dock_task(state, DOCK_TASK_WASH_MOP)


def test_unmapped_dock_activity_blocks_start_and_stop() -> None:
    """Unknown station activity is not treated as safe idle state."""
    state = _docked_state()
    state.station_activity = 99

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_stop_dock_task(state)


def test_dock_task_attributes_use_timer_progress() -> None:
    """Task switches expose coarse time-left and percent progress attributes."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=61,
        target=180,
        fields=("8", "9"),
    )
    switch = _switch(DOCK_TASK_DRY_MOP, state)

    assert switch.extra_state_attributes == {
        "time_left": "2m",
        "progress": 34,
    }


def test_dry_dust_bin_is_active_but_not_stoppable() -> None:
    """Dry dust-bin remains visible, but stop is blocked until its command is known."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=61,
        target=180,
        fields=("10", "11"),
    )
    switch = _switch(DOCK_TASK_DRY_DUST_BIN, state)

    assert switch.is_on
    assert switch.available
    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_DRY_DUST_BIN)
    assert switch.extra_state_attributes == {
        "time_left": "2m",
        "progress": 34,
    }


async def test_active_non_stoppable_task_rejects_turn_off() -> None:
    """Visible active dock tasks still reject unsafe stop requests."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=61,
        target=180,
        fields=("10", "11"),
    )
    coordinator = _coordinator(state)
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[3])

    with pytest.raises(HomeAssistantError, match="cannot be stopped"):
        await switch.async_turn_off()


def test_multiple_tasks_only_allow_scoped_stop() -> None:
    """Generic stop is unavailable for ambiguous multi-task dock activity."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=30,
        target=180,
        fields=("8", "9"),
    )
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_DRY_MOP)
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)


def test_clean_session_context_rejects_unscoped_dock_stop() -> None:
    """Generic force-end must not be exposed while a robot return is current."""
    state = _docked_state()
    state.working_status = WorkingStatus.TASK_COMPLETED
    state.station_activity = 1

    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_clean_session_context_allows_scoped_dock_bag_stop() -> None:
    """The scoped dock-bag payload remains safe during robot-side work."""
    state = _docked_state()
    state.working_status = WorkingStatus.TASK_COMPLETED
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)


def test_unmapped_coarse_activity_allows_scoped_dock_bag_stop() -> None:
    """Typed dock-bag telemetry can still be force-ended with stale coarse fields."""
    state = _docked_state()
    state.station_activity = 99
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not can_stop_dock_task(state)
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)
    assert not can_stop_dock_task(state, DOCK_TASK_DRY_MOP)


def test_robot_clean_start_allows_only_typed_dock_bag() -> None:
    """Robot starts are blocked by dock work except typed dock-bag drying."""
    state = _docked_state()
    state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)
    assert not can_start_robot_clean(state)

    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )
    assert can_start_robot_clean(state)


async def test_wash_mop_switch_falls_back_to_status_gated_command() -> None:
    """The alternate wash-mop topic is tried when the primary topic rejects."""
    coordinator = _coordinator()
    coordinator.client.wash_mop = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
    )
    coordinator.client.wash_mop_by_robot_status = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[1])

    await switch.async_turn_on()

    coordinator.client.wash_mop.assert_awaited_once()
    coordinator.client.wash_mop_by_robot_status.assert_awaited_once()


async def test_successful_start_reserves_private_guard_when_post_refresh_fails() -> None:
    """Accepted starts block follow-up commands without publishing task state."""
    coordinator = _coordinator()
    coordinator.client.empty_dustbin = AsyncMock(
        side_effect=lambda: (
            coordinator.client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
            or CommandResponse(result_code=CommandResult.SUCCESS)
        )
    )
    refresh_calls = 0

    async def refresh_dock_status() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            coordinator.has_fresh_state = False
            return False
        return True

    coordinator.async_refresh_dock_status = AsyncMock(side_effect=refresh_dock_status)
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    await switch.async_turn_on()

    assert switch.is_on
    assert not switch.available
    coordinator.client.empty_dustbin.assert_awaited_once()


async def test_concurrent_starts_are_serialized_by_local_reservation() -> None:
    """Two start requests cannot both dispatch against the same idle state."""
    coordinator = _coordinator()
    empty_switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])
    wash_switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[1])
    coordinator.client.empty_dustbin = AsyncMock(
        side_effect=lambda: (
            coordinator.client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
            or CommandResponse(result_code=CommandResult.SUCCESS)
        )
    )
    coordinator.client.wash_mop = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)

    results = await asyncio.gather(
        empty_switch.async_turn_on(),
        wash_switch.async_turn_on(),
        return_exceptions=True,
    )

    assert not isinstance(results[0], Exception)
    assert isinstance(results[1], HomeAssistantError)
    coordinator.client.empty_dustbin.assert_awaited_once()
    coordinator.client.wash_mop.assert_not_awaited()


async def test_switch_blocks_command_when_preflight_refresh_fails() -> None:
    """A failed pre-command state refresh prevents sending a start command."""
    coordinator = _coordinator()
    coordinator.async_refresh_dock_status = AsyncMock(return_value=False)
    coordinator.client.empty_dustbin = AsyncMock()
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    coordinator.client.empty_dustbin.assert_not_awaited()
