"""Tests for Narwal sensor entity descriptions."""

from __future__ import annotations

import time

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.sensor import SENSOR_DESCRIPTIONS  # noqa: E402
from custom_components.narwal.narwal_client import NarwalState, WorkingStatus  # noqa: E402


_DESCS = {d.key: d for d in SENSOR_DESCRIPTIONS}


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
