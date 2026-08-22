"""Tests for the station fault/consumable binary sensors (value_fn logic)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402
from custom_components.narwal.binary_sensor import (  # noqa: E402
    BINARY_SENSOR_DESCRIPTIONS,
    NarwalDockedSensor,
)

_DESCS = {d.key: d for d in BINARY_SENSOR_DESCRIPTIONS}


def _docked_sensor(state: NarwalState) -> NarwalDockedSensor:
    coordinator = MagicMock()
    coordinator.data = state
    sensor = NarwalDockedSensor.__new__(NarwalDockedSensor)
    sensor.coordinator = coordinator
    return sensor


def test_error_gated_on_base_status_seen() -> None:
    """Error is unavailable until a base_status arrives, then reflects has_error."""
    fn = _DESCS["error"].value_fn
    assert fn(NarwalState()) is None  # no base_status yet
    s = NarwalState()
    s.update_from_base_status({"1": {}})  # healthy
    assert fn(s) is False
    s.update_from_base_status({"1": {"1": 7}})  # fault
    assert fn(s) is True


def test_error_clears_when_field_absent() -> None:
    """A recovered robot omits field 1 entirely (empty repeated) — the fault must clear, not stick."""
    fn = _DESCS["error"].value_fn
    s = NarwalState()
    s.update_from_base_status({"1": {"1": 7}})  # fault
    assert fn(s) is True
    s.update_from_base_status({"2": 0})  # next status drops field 1
    assert fn(s) is False
    assert s.error_codes == []


def test_error_attributes_expose_code_detail() -> None:
    """The error sensor surfaces code/level/detail + a help link when faulted."""
    desc = _DESCS["error"]
    s = NarwalState()
    s.update_from_base_status({"1": {"1": 2105, "2": 3, "3": b"wheel stuck"}})
    attrs = desc.attrs_fn(s)
    assert attrs["codes"] == [2105]
    assert attrs["level"] == 3
    assert attrs["detail"] == "wheel stuck"
    assert "code=2105" in attrs["help_url"]


def test_no_help_url_when_healthy() -> None:
    """No help_url attribute when there's no active error code."""
    desc = _DESCS["error"]
    s = NarwalState()
    s.update_from_base_status({"1": {}})
    assert "help_url" not in desc.attrs_fn(s)


def test_consumable_alert_sensors() -> None:
    """Maintenance/replacement sensors reflect the alert lists + name attributes."""
    maint, repl = _DESCS["maintenance_required"], _DESCS["replacement_required"]
    s = NarwalState()
    assert maint.value_fn(s) is None  # no base_status yet
    s.update_from_base_status({"2": 0})  # robot reachable
    s.update_from_consumable_info({"1": {"1": [1], "2": [2, 8]}})
    assert maint.value_fn(s) is True
    assert maint.attrs_fn(s)["items"] == ["dust box"]
    assert repl.value_fn(s) is True
    assert repl.attrs_fn(s)["items"] == ["mop", "dust bag"]
    s.update_from_consumable_info({"1": {}})
    assert maint.value_fn(s) is False
    assert repl.value_fn(s) is False


def test_tank_problem_states() -> None:
    """Tank/bag sensors: None until reported, ok at value 1, problem at ≥2."""
    cw = _DESCS["clean_water_tank"].value_fn
    sb = _DESCS["station_bag"].value_fn
    assert cw(NarwalState()) is None  # not reported -> unavailable
    s = NarwalState()
    s.update_from_base_status({"23": 1, "39": 1})  # both ok
    assert cw(s) is False
    assert sb(s) is False
    s.update_from_base_status({"23": 2, "39": 3})  # EMPTY / SUGGEST_REPLACE
    assert cw(s) is True
    assert sb(s) is True


def test_unspecified_is_not_a_problem() -> None:
    """Value 0 (UNSPECIFIED) must not read as a problem."""
    fn = _DESCS["sewage_tank"].value_fn
    s = NarwalState()
    s.update_from_base_status({"24": 0})
    assert fn(s) is False


def test_busy_and_setup_available_when_idle() -> None:
    """Idle/docked robots expose setup controls."""
    state = NarwalState()
    state.working_status = WorkingStatus.DOCKED

    assert _DESCS["busy"].value_fn(state) is False
    assert _DESCS["setup_available"].value_fn(state) is True


def test_busy_and_setup_unavailable_when_cleaning() -> None:
    """Active cleaning hides start-time setup controls."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING_ALT
    state.last_active_working_status_time = time.monotonic()

    assert _DESCS["busy"].value_fn(state) is True
    assert _DESCS["setup_available"].value_fn(state) is False


def test_busy_and_setup_unavailable_when_station_active() -> None:
    """Dock-side tasks also hide start-time setup controls."""
    state = NarwalState()
    state.working_status = WorkingStatus.DOCKED
    state.dry_mop_remaining_time = 1_800

    assert state.is_station_active
    assert _DESCS["busy"].value_fn(state) is True
    assert _DESCS["setup_available"].value_fn(state) is False


def test_setup_unavailable_when_state_unknown() -> None:
    """Unknown startup state is not treated as configurable."""
    state = NarwalState()

    assert state.working_status == WorkingStatus.UNKNOWN
    assert _DESCS["busy"].value_fn(state) is False
    assert _DESCS["setup_available"].value_fn(state) is False


def test_docked_sensor_on_when_charging_to_resume() -> None:
    """A mid-task recharge is physically dock-side even if working_status is cleaning."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING_ALT
    state.battery_level = 25
    state.battery_level_increasing = True
    state.last_battery_change_time = time.monotonic()
    state.dock_field11 = 3
    state.dock_field47 = 1

    assert state.is_charging_to_resume
    assert _docked_sensor(state).is_on is True


def test_docked_sensor_on_when_station_task_active() -> None:
    """Dock-side tasks imply the robot/dock should present as docked."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING_ALT
    state.last_active_working_status_time = time.monotonic()
    state.dry_mop_remaining_time = 12_503

    assert state.is_station_active
    assert _docked_sensor(state).is_on is True
