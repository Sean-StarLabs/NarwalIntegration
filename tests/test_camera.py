"""Tests for Narwal map camera native trajectory handling."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.camera import NarwalMapCamera  # noqa: E402
from custom_components.narwal.narwal_client.const import WorkingStatus  # noqa: E402
from custom_components.narwal.narwal_client.models import (  # noqa: E402
    MapData,
    MapDisplayData,
    NarwalState,
    ObstacleInfo,
    RoomInfo,
)


def _camera(state: NarwalState | None = None) -> NarwalMapCamera:
    """Return a map camera with only the fields needed by these tests."""
    state = state or NarwalState()
    camera = object.__new__(NarwalMapCamera)
    camera.coordinator = MagicMock()
    camera.coordinator.client.state = state
    camera.coordinator.config_entry.options = {}
    camera.hass = MagicMock()
    camera._cached_image = None
    camera._cache_key = ()
    camera._last_render_time = 0.0
    camera._render_count = 0
    camera._render_generation = 0
    camera._render_task = None
    camera._pending_render = None
    camera._remapping_static_key = None
    camera._base_map_image = None
    camera._base_map_key = ()
    camera._base_map_options_key = (True, True, True)
    camera._room_label_points = []
    camera.async_write_ha_state = MagicMock()
    return camera


def test_native_trajectory_grid_points_convert_and_dedupe() -> None:
    """display_map trajectory points are converted without creating local routes."""
    static_map = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
    )

    points = NarwalMapCamera._native_trajectory_grid_points(
        [(2.0, 3.0), (2.05, 3.05), (3.0, 4.0), (100.0, 100.0)],
        static_map,
    )

    assert points == [(2.0, 2.0), (4.0, 4.0)]


def test_trajectory_cache_key_changes_for_same_length_native_update() -> None:
    """Same-length native trajectory updates still invalidate the rendered image."""
    assert NarwalMapCamera._trajectory_cache_key(
        [(1.0, 1.0), (2.0, 2.0)]
    ) != NarwalMapCamera._trajectory_cache_key([(1.0, 1.0), (2.0, 3.0)])


def test_handle_update_passes_display_map_native_trail_to_renderer() -> None:
    """The camera renders only the native display_map trajectory."""
    state = NarwalState()
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        compressed_map=b"map",
    )
    state.map_display_data = MapDisplayData(
        robot_x=2.5,
        robot_y=3.5,
        robot_heading=90.0,
        trajectory=[(2.0, 3.0), (3.0, 4.0)],
    )
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    assert camera._schedule_render.call_args.args[2] == [(2.0, 2.0), (4.0, 4.0)]


def test_handle_update_without_native_trajectory_renders_no_trail() -> None:
    """No display_map trajectory means no fallback trail is drawn."""
    state = NarwalState()
    state.map_data = MapData(width=10, height=10, compressed_map=b"map")
    state.map_display_data = MapDisplayData(robot_x=1.0, robot_y=1.0)
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    assert camera._schedule_render.call_args.args[2] is None


def test_remapping_defers_previous_static_map_until_replacement_arrives() -> None:
    """The old static map is hidden while remapping reports unchanged map data."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    camera = _camera(state)

    assert camera._defer_static_map_while_remapping(state, ("old",))
    assert camera._defer_static_map_while_remapping(state, ("old",))
    assert not camera._defer_static_map_while_remapping(state, ("new",))

    state.working_status = WorkingStatus.DOCKED

    assert not camera._defer_static_map_while_remapping(state, ("new",))
    assert camera._remapping_static_key is None


def test_clear_cached_map_image_invalidates_pending_render() -> None:
    """Invalid map identity clears all cached render state."""
    camera = _camera()
    camera._cached_image = b"old"
    camera._cache_key = ("old",)
    camera._base_map_image = object()
    camera._base_map_key = ("old",)
    camera._pending_render = ("display", ("old",), None)

    camera._clear_cached_map_image()

    assert camera._cached_image is None
    assert camera._cache_key == ()
    assert camera._base_map_image is None
    assert camera._base_map_key == ()
    assert camera._pending_render is None
    assert camera._render_generation == 1


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
    camera = _camera()
    running = MagicMock()
    running.done.return_value = False
    camera._render_task = running

    camera._schedule_render("display", ("new",), [(1.0, 2.0)])

    assert camera._pending_render == ("display", ("new",), [(1.0, 2.0)])
    running.done.assert_called_once_with()


@pytest.mark.asyncio
async def test_render_overlay_receives_native_trail_argument() -> None:
    """Executor rendering receives the native display-map trail directly."""
    state = NarwalState()
    static_map = MapData(
        width=10,
        height=10,
        resolution=50,
        origin_x=0,
        origin_y=0,
        compressed_map=b"\x01",
    )
    state.map_data = static_map
    camera = _camera(state)
    camera._base_map_image = object()
    camera._base_map_key = NarwalMapCamera._static_map_key(static_map)
    camera._base_map_options_key = (False, False, False)
    camera._render_generation = 1
    camera._map_option = lambda _key: False
    camera._map_rotation = lambda: 0
    camera._map_zoom = lambda: 1.0
    display = MapDisplayData(robot_x=2.0, robot_y=2.0, robot_heading=90.0)
    native_trail = [(1.0, 1.0), (2.0, 2.0)]
    captured = {}

    async def async_add_executor_job(_func, *args):
        captured["trail"] = args[5]
        return b"png"

    camera.hass.async_add_executor_job = async_add_executor_job

    await camera._async_render(display, ("render-key",), 1, native_trail)

    assert captured["trail"] == native_trail
    assert camera._cached_image == b"png"
    assert camera._cache_key == ("render-key",)
    assert camera._render_count == 1


def test_extra_state_attributes_count_display_trajectory() -> None:
    """Diagnostics expose native display-map trajectory size only."""
    state = NarwalState()
    state.map_display_data = MapDisplayData(trajectory=[(1.0, 1.0), (2.0, 2.0)])
    camera = _camera(state)
    camera._render_count = 3

    assert camera.extra_state_attributes == {
        "render_count": 3,
        "native_trajectory_points": 2,
    }
