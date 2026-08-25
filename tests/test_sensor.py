"""Tests for Narwal sensor entities."""

from __future__ import annotations

from unittest.mock import MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.sensor import (  # noqa: E402
    SENSOR_DESCRIPTIONS,
    NarwalSensor,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402


def _sensor(key: str, state: NarwalState) -> NarwalSensor:
    """Create a NarwalSensor with mocked coordinator data."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_device"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.state.firmware_version = "1.0.0"
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
    return NarwalSensor(coordinator, description)


def test_current_room_is_not_a_standalone_sensor() -> None:
    """Current room is live vacuum context, not a separate stale sensor."""
    assert "current_room" not in {description.key for description in SENSOR_DESCRIPTIONS}


def test_cleaning_metrics_are_unavailable_when_idle() -> None:
    """Cleaning metric sensors should not expose stale previous-clean values."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.cleaning_area = 12.5
    state.cleaning_time = 900
    state.task_remaining_time = 300

    assert not _sensor("cleaning_area", state).available
    assert not _sensor("cleaning_time", state).available
    assert not _sensor("remaining_time", state).available


def test_cleaning_metrics_are_available_during_active_clean() -> None:
    """Cleaning metric sensors expose current robot task values while active."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.cleaning_area = 12.5
    state.cleaning_time = 900
    state.task_remaining_time = 300

    assert _sensor("cleaning_area", state).available
    assert _sensor("cleaning_area", state).native_value == 12.5
    assert _sensor("cleaning_time", state).available
    assert _sensor("cleaning_time", state).native_value == 900
    assert _sensor("remaining_time", state).available
    assert _sensor("remaining_time", state).native_value == 300
