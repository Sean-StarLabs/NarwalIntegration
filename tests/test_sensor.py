"""Tests for Narwal sensor entity descriptions."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.narwal_client import (  # noqa: E402
    MapData,
    NarwalState,
    RoomInfo,
    WorkingStatus,
)
from custom_components.narwal.sensor import (  # noqa: E402
    _MAX_MAP_METADATA_ATTRIBUTE_BYTES,
    SENSOR_DESCRIPTIONS,
    NarwalMapMetadataSensor,
    NarwalSensor,
    NarwalTaskStatusSensor,
    _fit_map_metadata_attributes,
    _json_size_bytes,
)

_DESCS = {d.key: d for d in SENSOR_DESCRIPTIONS}


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


def test_transient_sensor_unavailable_when_value_missing() -> None:
    """Transient sensors are unavailable while their backing metric is absent."""
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.data = NarwalState()
    sensor = NarwalSensor(coordinator, _DESCS["current_room"])

    assert sensor.native_value is None
    assert not sensor.available


def test_current_room_hidden_without_active_clean_metrics() -> None:
    """Current room is stale once live cleaning metrics are no longer active."""
    state = NarwalState()
    state.current_room_id = 4
    state.current_room_aux_name = "Kitchen"

    assert _DESCS["current_room"].value_fn(state) is None


def test_current_room_available_during_active_clean_metrics() -> None:
    """Current room is exposed while the robot is actively reporting a clean."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING
    state.last_active_working_status_time = time.monotonic()
    state.current_room_id = 4
    state.current_room_aux_name = "Kitchen"

    assert _DESCS["current_room"].value_fn(state) == "Kitchen"


def test_task_progress_available_while_paused() -> None:
    """Paused clean sessions keep showing progress metrics."""
    state = NarwalState()
    state.is_paused = True
    state.task_progress_percent = 64

    assert _DESCS["task_progress"].value_fn(state) == 64


def test_task_status_sensor_reports_parsed_fault_as_error() -> None:
    """Parsed base-status faults take precedence over ordinary task phases."""
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.data = NarwalState(working_status=WorkingStatus.CLEANING)
    coordinator.data.has_error = True

    sensor = NarwalTaskStatusSensor(coordinator)

    assert sensor.native_value == "error"


def test_map_metadata_unavailable_until_map_loaded() -> None:
    """Map metadata should not be available with an unknown native value."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = NarwalState()
    sensor = NarwalMapMetadataSensor.__new__(NarwalMapMetadataSensor)
    sensor.coordinator = coordinator

    assert sensor.native_value is None
    assert not sensor.available


def test_map_metadata_unavailable_for_empty_map_id() -> None:
    """Empty map payloads use map_id=0 and should not expose metadata."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = NarwalState()
    coordinator.data.map_data = MapData(map_id=0)
    sensor = NarwalMapMetadataSensor.__new__(NarwalMapMetadataSensor)
    sensor.coordinator = coordinator

    assert sensor.native_value is None
    assert not sensor.available


def test_map_metadata_attributes_fall_back_when_compact_payload_is_still_large() -> None:
    """Oversized metadata is truncated beyond the one-pass geometry removal."""
    room_name = "Room " + ("x" * _MAX_MAP_METADATA_ATTRIBUTE_BYTES)
    attributes = {
        "map_size": {"width": 10, "height": 10},
        "map_resolution": 60,
        "rooms": [
            {
                "id": 1,
                "name": room_name,
                "room_type": 3,
                "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
                "label": {"x": 5, "y": 5},
                "polygons": [[{"x": 0, "y": 0}]],
            }
        ],
        "rugs": [],
    }

    fitted = _fit_map_metadata_attributes(attributes)

    assert fitted["geometry_omitted"] is True
    assert fitted["metadata_truncated"] is True
    assert fitted["rooms"] == []
    assert _json_size_bytes(fitted) <= _MAX_MAP_METADATA_ATTRIBUTE_BYTES


def test_map_metadata_omits_geometry_when_attribute_payload_is_too_large() -> None:
    """Large room geometry stays below HA's state attribute size limit."""
    state = NarwalState()
    state.map_data = MapData(
        width=1000,
        height=1000,
        resolution=60,
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
        room_bounds={4: (0, 0, 999, 999)},
        room_centers={4: (500.0, 500.0)},
        room_polygons={
            4: [[(float(index), float(index % 100)) for index in range(3000)]],
        },
    )
    coordinator = MagicMock()
    coordinator.data = state
    sensor = NarwalMapMetadataSensor.__new__(NarwalMapMetadataSensor)
    sensor.coordinator = coordinator

    attrs = sensor.extra_state_attributes

    assert attrs is not None
    assert attrs["geometry_omitted"] is True
    assert attrs["geometry_omitted_reason"] == "attribute_size"
    assert "polygons" not in attrs["rooms"][0]


def test_map_metadata_omits_room_surface_when_unknown() -> None:
    """Room metadata should not invent hard-floor when no surface source exists."""
    state = NarwalState()
    state.map_data = MapData(
        width=10,
        height=10,
        resolution=60,
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
        room_bounds={4: (0, 0, 9, 9)},
        room_centers={4: (5.0, 5.0)},
        room_polygons={4: [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]},
    )
    coordinator = MagicMock()
    coordinator.data = state
    sensor = NarwalMapMetadataSensor.__new__(NarwalMapMetadataSensor)
    sensor.coordinator = coordinator

    attrs = sensor.extra_state_attributes

    assert attrs is not None
    assert "surface" not in attrs["rooms"][0]
