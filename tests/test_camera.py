"""Tests for Narwal camera trail session handling."""

from __future__ import annotations
from unittest.mock import MagicMock, patch

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.camera import (  # noqa: E402
    NarwalMapCamera,
    _is_cleaning_for_trail,
    _is_terminal_for_trail,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, NarwalState, ObstacleInfo, RoomInfo  # noqa: E402


def _camera_with_reset(reset) -> NarwalMapCamera:
    """Return a camera object with only the reset hook needed by this test."""
    camera = object.__new__(NarwalMapCamera)
    camera._reset_trail = reset
    camera._cached_image = b"old"
    camera._cache_key = ("old",)
    camera._base_map_image = object()
    camera._base_map_key = ("old",)
    camera._pending_render = ("display", ("old",))
    camera._render_generation = 0
    camera._remapping_seen = False
    camera._remapping_static_key = None
    return camera


def test_sync_trail_session_keeps_trail_across_short_status_flap() -> None:
    """A brief non-cleaning status during the first minute is not a new session."""
    state = NarwalState()
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_active = True
    state.cleaning_trail_last_cleaning_time = 30
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    with patch("custom_components.narwal.camera.time.monotonic", return_value=100.0):
        camera._sync_trail_session(state, False)

    state.cleaning_time = 35
    with patch("custom_components.narwal.camera.time.monotonic", return_value=105.0):
        camera._sync_trail_session(state, True)

    reset.assert_not_called()
    assert state.cleaning_trail == [(1.0, 1.0)]


def test_sync_trail_session_resets_when_cleaning_time_rewinds() -> None:
    """A lower non-zero cleaning timer means a genuinely new task started."""
    state = NarwalState()
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_active = True
    state.cleaning_trail_last_cleaning_time = 120
    state.cleaning_time = 5
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    camera._sync_trail_session(state, True)

    reset.assert_called_once_with()
    assert state.cleaning_trail == []


def test_sync_trail_session_resets_terminal_to_active_with_zero_timer() -> None:
    """A new session from dock must not keep the old trail while timer is zero."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_active = True
    state.cleaning_trail_last_cleaning_time = 120
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    with patch("custom_components.narwal.camera.time.monotonic", return_value=100.0):
        camera._sync_trail_session(state, False)

    state.working_status = WorkingStatus.CLEANING
    state.cleaning_time = 0
    with patch("custom_components.narwal.camera.time.monotonic", return_value=105.0):
        camera._sync_trail_session(state, True)

    reset.assert_called_once_with()
    assert state.cleaning_trail == []
    assert state.cleaning_trail_terminal_since == 0.0


def test_charging_to_resume_is_not_terminal_for_trail() -> None:
    """Docked charge-to-resume phases are still part of the same clean."""
    state = MagicMock()
    state.is_charging_to_resume = True

    assert not _is_terminal_for_trail(state)


def test_remapping_is_not_a_cleaning_trail_session() -> None:
    """Remapping keeps map updates active but must not extend old cleaning trails."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    state.last_active_working_status_time = 1.0

    with patch("custom_components.narwal.camera.time.monotonic", return_value=2.0):
        assert not _is_cleaning_for_trail(state)


def test_remapping_clears_retained_cleaning_trail() -> None:
    """A replacement map must not inherit the previous cleaning trail."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_map_key = ("old",)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    camera._sync_trail_map(state, ("new",), False)

    reset.assert_called_once_with()
    assert state.cleaning_trail == []
    assert state.cleaning_trail_map_key is None
    assert camera._cached_image is None
    assert camera._cache_key == ()
    assert camera._base_map_image is None
    assert camera._base_map_key == ()
    assert camera._pending_render is None
    assert camera._render_generation == 1


def test_remapping_clears_retained_trail_before_static_map_validation() -> None:
    """Remapping must clear stale trails even before usable map data arrives."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_map_key = ("old",)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    assert camera._clear_trail_if_remapping(state)

    reset.assert_called_once_with()
    assert state.cleaning_trail == []
    assert state.cleaning_trail_map_key is None
    assert camera._cached_image is None
    assert camera._cache_key == ()
    assert camera._base_map_image is None
    assert camera._base_map_key == ()
    assert camera._pending_render is None
    assert camera._render_generation == 1


def test_remapping_clears_cached_image_without_retained_trail() -> None:
    """A prior trail reset must not leave the old map PNG visible during remap."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    assert camera._clear_trail_if_remapping(state)

    reset.assert_not_called()
    assert camera._cached_image is None
    assert camera._cache_key == ()
    assert camera._base_map_image is None
    assert camera._base_map_key == ()
    assert camera._pending_render is None
    assert camera._render_generation == 1


def test_remapping_does_not_clear_cache_on_every_update() -> None:
    """A valid replacement map can render while the robot remains in REMAPPING."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    assert camera._clear_trail_if_remapping(state)

    camera._cached_image = b"new"
    camera._cache_key = ("new",)
    camera._base_map_image = object()
    camera._base_map_key = ("new",)
    camera._pending_render = ("display", ("new",))

    assert camera._clear_trail_if_remapping(state)

    reset.assert_not_called()
    assert camera._cached_image == b"new"
    assert camera._cache_key == ("new",)
    assert camera._base_map_image is not None
    assert camera._base_map_key == ("new",)
    assert camera._pending_render == ("display", ("new",))
    assert camera._render_generation == 1


def test_remapping_cache_clear_rearms_after_remapping_ends() -> None:
    """The next REMAPPING phase must still clear the previous map image once."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    assert camera._clear_trail_if_remapping(state)
    state.working_status = WorkingStatus.DOCKED
    assert not camera._clear_trail_if_remapping(state)

    camera._cached_image = b"new"
    camera._cache_key = ("new",)
    state.working_status = WorkingStatus.REMAPPING

    assert camera._clear_trail_if_remapping(state)

    assert camera._cached_image is None
    assert camera._cache_key == ()
    assert camera._render_generation == 2


def test_remapping_defers_previous_static_map_until_replacement_arrives() -> None:
    """The old static map must not be re-rendered during an active remap."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    assert camera._defer_static_map_while_remapping(state, ("old",))
    assert camera._defer_static_map_while_remapping(state, ("old",))
    assert not camera._defer_static_map_while_remapping(state, ("new",))

    state.working_status = WorkingStatus.DOCKED

    assert not camera._defer_static_map_while_remapping(state, ("new",))
    assert camera._remapping_static_key is None


def test_static_map_change_clears_retained_cleaning_trail() -> None:
    """Retained trail coordinates are valid only for their original map."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_map_key = ("old",)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    camera._sync_trail_map(state, ("new",), False)

    reset.assert_called_once_with()
    assert state.cleaning_trail == []
    assert state.cleaning_trail_map_key is None


def test_record_trail_position_uses_capped_jump_threshold() -> None:
    """Large maps still have a bounded jump threshold for trail smoothing."""
    state = NarwalState()
    camera = object.__new__(NarwalMapCamera)
    camera.coordinator = MagicMock()
    camera.coordinator.client.state = state

    with patch("custom_components.narwal.camera.time.monotonic", side_effect=[10, 14]):
        camera._record_trail_position(10.0, 10.0, 2000, 2000)
        camera._record_trail_position(90.0, 10.0, 2000, 2000)

    assert state.cleaning_trail == [(10.0, 10.0), (90.0, 10.0)]


def test_static_map_key_distinguishes_content_with_same_timestamp() -> None:
    """Different floor data with the same created_at must rebuild the base image."""
    first = MapData(map_id=1, created_at=0, width=2, height=2, compressed_map=b"\x01")
    second = MapData(map_id=1, created_at=0, width=2, height=2, compressed_map=b"\x02")

    assert NarwalMapCamera._static_map_key(first) != NarwalMapCamera._static_map_key(
        second
    )


def test_static_map_key_distinguishes_room_label_changes() -> None:
    """Room-name changes affect rendered labels and must rebuild the base image."""
    first = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
    )
    second = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        rooms=[RoomInfo(room_id=4, name="Lounge")],
    )

    assert NarwalMapCamera._static_map_key(first) != NarwalMapCamera._static_map_key(
        second
    )
    assert NarwalMapCamera._static_map_trail_key(first) == (
        NarwalMapCamera._static_map_trail_key(second)
    )


def test_room_label_change_does_not_clear_retained_cleaning_trail() -> None:
    """Room renames rebuild labels without invalidating retained trail coordinates."""
    first = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
    )
    second = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        rooms=[RoomInfo(room_id=4, name="Lounge")],
    )
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.cleaning_trail.append((1.0, 1.0))
    state.cleaning_trail_map_key = NarwalMapCamera._static_map_trail_key(first)
    reset = MagicMock(side_effect=state.reset_cleaning_trail)
    camera = _camera_with_reset(reset)

    camera._sync_trail_map(
        state,
        NarwalMapCamera._static_map_trail_key(second),
        False,
    )

    reset.assert_not_called()
    assert state.cleaning_trail == [(1.0, 1.0)]


def test_static_map_key_distinguishes_obstacle_changes() -> None:
    """Furniture metadata affects the rendered base map."""
    first = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        obstacles=[ObstacleInfo(id=1, type_id=14, center_x=10.0, center_y=10.0)],
    )
    second = MapData(
        map_id=1,
        width=2,
        height=2,
        compressed_map=b"\x01",
        obstacles=[ObstacleInfo(id=1, type_id=28, center_x=10.0, center_y=10.0)],
    )

    assert NarwalMapCamera._static_map_key(first) != NarwalMapCamera._static_map_key(
        second
    )


def test_schedule_render_coalesces_when_render_already_running() -> None:
    """Broadcast bursts should keep only the latest pending render request."""
    camera = object.__new__(NarwalMapCamera)
    running = MagicMock()
    running.done.return_value = False
    camera._render_task = running
    camera._pending_render = None

    camera._schedule_render("display", ("new",))

    assert camera._pending_render == ("display", ("new",))
    running.done.assert_called_once_with()
