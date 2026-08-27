"""Tests for Narwal map camera trail handling."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock, patch

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.camera import (  # noqa: E402
    _NATIVE_TRAIL_MAX_POINTS,
    NarwalMapCamera,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, MapDisplayData, NarwalState  # noqa: E402


def _float_stream(*values: float) -> bytes:
    """Encode a packed float32 stream as display_map field 2 uses it."""
    return b"".join(struct.pack("<f", value) for value in values)


def _camera(state: NarwalState) -> NarwalMapCamera:
    """Create a NarwalMapCamera with mocked coordinator and render hook."""
    camera = object.__new__(NarwalMapCamera)
    camera.coordinator = MagicMock()
    camera.coordinator.client.state = state
    camera.coordinator.client.get_robot_debug_image = AsyncMock(return_value=None)
    camera.coordinator.config_entry = MagicMock()
    camera.coordinator.config_entry.options = {}
    camera.hass = MagicMock()
    camera.hass.async_create_task = MagicMock()
    camera.async_write_ha_state = MagicMock()
    camera.async_update_token = MagicMock()
    camera._cached_image = None
    camera._cache_key = ()
    camera._last_render_time = 0.0
    camera._pending_render = None
    camera._render_task = None
    camera._async_render = MagicMock(return_value="render-task")
    camera._base_map_image = None
    camera._base_map_ts = 0
    camera._base_map_options_key = (True, False, False, ((), ()))
    camera._room_label_points = []
    camera._carpet_map_image = None
    camera._carpet_map_signature = ()
    camera._base_carpet_map_signature = ()
    camera._carpet_map_last_fetch = 0.0
    return camera


def test_native_trajectory_grid_points_convert_and_dedupe() -> None:
    """Narwal-native trajectory points are converted using the map origin."""
    static_map = MapData(width=100, height=100, origin_x=2, origin_y=4)

    points = NarwalMapCamera._native_trajectory_grid_points(
        [(3.0, 5.0), (3.1, 5.1), (4.0, 6.0), (200.0, 200.0)],
        static_map,
    )

    assert points == [(1.0, 1.0), (2.0, 2.0)]


def test_trajectory_cache_key_changes_for_same_length_native_update() -> None:
    """Same-length native trajectory updates must repaint the map overlay."""
    first = MapDisplayData.from_broadcast(
        {"2": {"1": _float_stream(1.0, 2.0), "2": _float_stream(1.0, 2.0)}}
    )
    corrected = MapDisplayData.from_broadcast(
        {"2": {"1": _float_stream(1.0, 3.0), "2": _float_stream(1.0, 2.0)}}
    )

    assert NarwalMapCamera._trajectory_cache_key(first) != (
        NarwalMapCamera._trajectory_cache_key(corrected)
    )


def test_native_trajectory_grid_points_decimates_long_route_and_preserves_tail() -> None:
    """Long native trajectories retain the latest Narwal point when capped."""
    static_map = MapData(width=_NATIVE_TRAIL_MAX_POINTS + 20, height=10)
    points = [(float(index), 1.0) for index in range(_NATIVE_TRAIL_MAX_POINTS + 10)]

    converted = NarwalMapCamera._native_trajectory_grid_points(points, static_map)

    assert len(converted) == _NATIVE_TRAIL_MAX_POINTS
    assert converted[0] == (0.0, 1.0)
    assert converted[-1] == (float(_NATIVE_TRAIL_MAX_POINTS + 9), 1.0)
    assert converted[-200:] == [
        (float(index), 1.0)
        for index in range(_NATIVE_TRAIL_MAX_POINTS - 190, _NATIVE_TRAIL_MAX_POINTS + 10)
    ]


def test_handle_update_passes_native_trail_to_renderer_outside_cleaning() -> None:
    """Narwal-native accumulated trajectory is rendered whenever present."""
    state = NarwalState(working_status=WorkingStatus.CHARGED)
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData.from_broadcast(
        {
            "1": {"1": {"1": 4.0, "2": 6.0}},
            "2": {
                "1": _float_stream(3.0, 4.0),
                "2": _float_stream(5.0, 6.0),
            },
        }
    )
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    with patch.object(
        NarwalMapCamera,
        "_native_trajectory_grid_points",
        side_effect=AssertionError("callback must not process full native route"),
    ):
        camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    render_display, render_key = camera._schedule_render.call_args.args
    assert render_display is state.map_display_data
    assert render_key[-1] == state.map_display_data.trajectory_signature
    camera.hass.async_create_task.assert_not_called()


def test_handle_update_without_native_trajectory_renders_no_trail() -> None:
    """A live robot position alone must not draw a trail."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(robot_x=4.0, robot_y=6.0)
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    render_display, render_key = camera._schedule_render.call_args.args
    assert render_display is state.map_display_data
    assert render_key[-1] == ()


def test_schedule_render_coalesces_while_render_is_running() -> None:
    """Only the latest native trajectory render is kept while a render is active."""
    camera = _camera(NarwalState())
    running_task = MagicMock()
    running_task.done.return_value = False
    camera._render_task = running_task
    first = MapDisplayData(robot_x=1.0)
    latest = MapDisplayData(robot_x=2.0)

    camera._schedule_render(first, ("first",))
    camera._schedule_render(latest, ("latest",))

    assert camera._pending_render == (latest, ("latest",))
    camera.hass.async_create_task.assert_not_called()


def test_handle_update_replaces_pending_render_when_state_returns_to_cache() -> None:
    """A rollback to the cached key cancels any queued intermediate render."""
    state = NarwalState()
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        created_at=123,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(robot_x=4.0, robot_y=6.0)
    camera = _camera(state)
    cached_key = (
        123,
        (True, False, False, ((), ())),
        (0, 1.0),
        4.0,
        6.0,
        0.0,
        (),
    )
    camera._cached_image = b"old"
    camera._cache_key = cached_key
    running_task = MagicMock()
    running_task.done.return_value = False
    camera._render_task = running_task
    camera._pending_render = (MapDisplayData(robot_x=9.0), ("intermediate",))

    camera._handle_coordinator_update()

    assert camera._pending_render == (state.map_display_data, cached_key)
    camera.hass.async_create_task.assert_not_called()


async def test_render_pending_delays_throttled_native_update() -> None:
    """Throttled rendering coalesces again after waiting."""
    camera = _camera(NarwalState())
    camera._cached_image = b"old"
    camera._cache_key = (1, (True, True, True), (0, 1.0), 1.0)
    camera._last_render_time = 100.0
    first = MapDisplayData(robot_x=2.0)
    first_key = (1, (True, True, True), (0, 1.0), 2.0)
    latest = MapDisplayData(robot_x=3.0)
    latest_key = (1, (True, True, True), (0, 1.0), 3.0)
    camera._pending_render = (
        first,
        first_key,
    )
    camera._async_render = AsyncMock()

    async def queue_latest(_delay: float) -> None:
        camera._pending_render = (latest, latest_key)

    with (
        patch("custom_components.narwal.camera.time.monotonic", return_value=101.0),
        patch(
            "custom_components.narwal.camera.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=queue_latest,
        ) as sleep,
    ):
        await camera._async_render_pending()

    sleep.assert_awaited_once_with(1.0)
    camera._async_render.assert_awaited_once_with(latest, latest_key)
    assert camera._render_task is None


async def test_render_updates_camera_token_for_new_snapshot_url() -> None:
    """Rendered map frames rotate the camera token so picture cards reload."""
    state = NarwalState()
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        created_at=123,
        compressed_map=b"\x01",
    )
    camera = _camera(state)
    camera._async_render = NarwalMapCamera._async_render.__get__(
        camera, NarwalMapCamera
    )

    async def executor_job(fn, *args):
        if getattr(fn, "__name__", "") == "render_base_map":
            return object()
        if getattr(fn, "__name__", "") == "room_label_points":
            return []
        return b"png"

    camera.hass.async_add_executor_job = AsyncMock(side_effect=executor_job)

    with patch.object(NarwalMapCamera, "_render_overlay_image", return_value=b"png"):
        await camera._async_render(MapDisplayData(robot_x=4.0, robot_y=6.0), ("key",))

    assert camera._cached_image == b"png"
    camera.async_update_token.assert_called_once()
    camera.async_write_ha_state.assert_called_once()
