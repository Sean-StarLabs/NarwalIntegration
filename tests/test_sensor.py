"""Tests for Narwal sensor entities."""

from __future__ import annotations

from unittest.mock import MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.sensor import SENSOR_DESCRIPTIONS, NarwalSensor  # noqa: E402
from custom_components.narwal.narwal_client import NarwalState  # noqa: E402

_DESCS = {description.key: description for description in SENSOR_DESCRIPTIONS}


def test_battery_zero_is_available_when_base_status_reports_it() -> None:
    """A real 0% battery reading should not be treated as missing data."""
    state = NarwalState()
    state.update_from_base_status({"2": 0})

    assert _DESCS["battery"].value_fn(state) == 0


def test_non_transient_sensor_stays_available_when_value_missing() -> None:
    """Missing diagnostics should report unknown, not unavailable."""
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client.state.firmware_version = ""
    coordinator.last_update_success = True
    coordinator.data = NarwalState()
    sensor = NarwalSensor(coordinator, _DESCS["firmware_version"])

    assert sensor.native_value is None
    assert sensor.available


def test_station_task_sensor_reports_idle_when_station_healthy() -> None:
    """Idle station state is a real state, not an unavailable sensor."""
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.data = NarwalState()
    sensor = NarwalSensor(coordinator, _DESCS["station_task"])

    assert sensor.native_value == "idle"
    assert sensor.available
