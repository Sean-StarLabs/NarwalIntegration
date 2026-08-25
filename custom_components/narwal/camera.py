"""Map camera entity for Narwal vacuum — MJPEG streaming for live updates."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    CONF_MAP_ROTATION,
    CONF_MAP_ZOOM,
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
    MAP_ROTATION_DEFAULT,
    MAP_ZOOM_DEFAULT,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.const import WorkingStatus

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders (display_map arrives every ~1.5s
# but PIL rendering is CPU-bound — no need to render every broadcast).
_MIN_RENDER_INTERVAL = 2

# Native trajectory points can repeat across display_map frames because field 2 is
# the accumulated Narwal trajectory. Deduplicate tiny adjacent moves only.
_NATIVE_TRAIL_MIN_GRID_DELTA = 0.25
_NATIVE_TRAIL_MAX_POINTS = 50000


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal camera entities."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapCamera(coordinator), NarwalCarpetCamera(coordinator)])


class NarwalCarpetCamera(NarwalEntity, Camera):
    """On-demand camera showing the robot's carpet-detection debug image.

    Backed by developer/get_robot_debug_image, which returns cleartext PNGs (no
    crypto). Images are only produced while the robot is mapping/cleaning, so this
    only polls in an active state and otherwise serves the last image.
    """

    _attr_name = "Carpet map"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the carpet debug camera entity."""
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_carpet_map"
        self._cached: bytes | None = None
        self._cached_at: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """Return the latest carpet-detection PNG; poll only while active."""
        state = self.coordinator.data
        active = state is not None and (
            state.is_cleaning or state.working_status == WorkingStatus.REMAPPING
        )
        if not active:
            return self._cached  # keep last image; don't hit the WS while idle
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < 8.0:
            return self._cached
        img = await self.coordinator.client.get_robot_debug_image()
        if img is not None:
            self._cached = img
            self._cached_at = now
        return self._cached


class NarwalMapCamera(NarwalEntity, Camera):
    """Camera entity that streams the vacuum's map as MJPEG."""

    _attr_name = "Map"
    _attr_is_streaming = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the map camera entity."""
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map"
        self._cached_image: bytes | None = None
        self._cache_key: tuple = ()
        self._last_render_time: float = 0.0
        self._render_count: int = 0
        self._render_generation: int = 0
        self._render_task: asyncio.Task | None = None
        self._pending_render: tuple | None = None
        self._remapping_static_key: tuple | None = None
        # Cached base map (PIL Image) — only re-rendered when static map changes
        self._base_map_image = None  # PIL Image or None
        self._base_map_key: tuple = ()
        self._base_map_options_key: tuple[bool, bool, bool] = (
            MAP_OPTION_DEFAULTS[CONF_SHOW_ROOM_LABELS],
            MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE],
            MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE_LABELS],
        )
        self._room_label_points: list[tuple[str, float, float]] = []

    @staticmethod
    def _static_map_geometry_key(static_map) -> tuple:
        """Return a cache key for map geometry and coordinate identity."""
        compressed = static_map.compressed_map
        if isinstance(compressed, bytes):
            digest_data = compressed
        elif isinstance(compressed, bytearray):
            digest_data = bytes(compressed)
        else:
            try:
                digest_data = bytes(compressed)
            except (TypeError, ValueError):
                digest_data = repr(compressed).encode("utf-8", errors="replace")
        digest = hashlib.blake2s(digest_data, digest_size=8).hexdigest()
        return (
            getattr(static_map, "map_id", None),
            static_map.created_at or 0,
            static_map.width,
            static_map.height,
            static_map.resolution,
            static_map.origin_x,
            static_map.origin_y,
            digest,
        )

    @staticmethod
    def _static_map_key(static_map) -> tuple:
        """Return a cache key for rendered static map content."""
        base_key = NarwalMapCamera._static_map_geometry_key(static_map)
        room_key = tuple(
            (room.room_id, room.display_name) for room in getattr(static_map, "rooms", ())
        )
        obstacle_key = tuple(
            (
                obstacle.id,
                obstacle.type_id,
                obstacle.center_x,
                obstacle.center_y,
                obstacle.width,
                obstacle.height,
                obstacle.angle,
            )
            for obstacle in getattr(static_map, "obstacles", ())
        )
        return (*base_key, room_key, obstacle_key)

    @staticmethod
    def _cleaned_area_key(cleaned_area) -> tuple | None:
        """Return a compact render cache key for a native cleaned-area overlay."""
        if cleaned_area is None:
            return None
        digest = hashlib.blake2s(cleaned_area.compressed_map, digest_size=8).hexdigest()
        return (
            cleaned_area.width,
            cleaned_area.height,
            cleaned_area.origin_x,
            cleaned_area.origin_y,
            digest,
        )

    def _map_option(self, key: str) -> bool:
        """Return a persisted map display option."""
        return bool(
            self.coordinator.config_entry.options.get(
                key,
                MAP_OPTION_DEFAULTS[key],
            )
        )

    def _map_rotation(self) -> int:
        """Return persisted clockwise map rotation in degrees."""
        try:
            rotation = int(
                self.coordinator.config_entry.options.get(
                    CONF_MAP_ROTATION,
                    MAP_ROTATION_DEFAULT,
                )
            )
        except (TypeError, ValueError):
            return MAP_ROTATION_DEFAULT
        rotation %= 360
        return rotation if rotation in (0, 90, 180, 270) else MAP_ROTATION_DEFAULT

    def _map_zoom(self) -> float:
        """Return persisted map zoom factor."""
        try:
            zoom = float(
                self.coordinator.config_entry.options.get(
                    CONF_MAP_ZOOM,
                    MAP_ZOOM_DEFAULT,
                )
            )
        except (TypeError, ValueError):
            return MAP_ZOOM_DEFAULT
        return max(1.0, min(2.0, zoom))

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """Return the latest map image as PNG (for snapshot/polling clients)."""
        return self._cached_image

    async def handle_async_mjpeg_stream(self, request):
        """Stream map as MJPEG using HA's built-in still-image streamer."""
        from homeassistant.components.camera import async_get_still_stream

        return await async_get_still_stream(
            request, self.async_camera_image, "image/png", _MIN_RENDER_INTERVAL,
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int]:
        """Expose compact render stats for MJPEG refresh and diagnostics."""
        state = self.coordinator.client.state
        display = state.map_display_data
        return {
            "render_count": self._render_count,
            "native_trajectory_points": len(display.trajectory) if display else 0,
        }

    def _clear_cached_map_image(self) -> None:
        """Drop cached map images after map identity becomes invalid."""
        self._cached_image = None
        self._cache_key = ()
        self._base_map_image = None
        self._base_map_key = ()
        self._pending_render = None
        self._render_generation += 1

    def _defer_static_map_while_remapping(
        self,
        state,
        static_key: tuple,
    ) -> bool:
        """Return True while a remap is still reporting the previous static map."""
        if state.working_status != WorkingStatus.REMAPPING:
            self._remapping_static_key = None
            return False
        if self._remapping_static_key is None:
            self._remapping_static_key = static_key
            return True
        return static_key == self._remapping_static_key

    @staticmethod
    def _native_trajectory_grid_points(
        trajectory: list[tuple[float, float]],
        static_map,
    ) -> list[tuple[float, float]]:
        """Return native trajectory points converted to in-bounds grid coordinates."""
        if static_map.resolution <= 0:
            return []

        scale = 100.0 / static_map.resolution
        points: list[tuple[float, float]] = []
        for world_x, world_y in trajectory:
            grid_x = world_x * scale - static_map.origin_x
            grid_y = world_y * scale - static_map.origin_y
            if not math.isfinite(grid_x) or not math.isfinite(grid_y):
                continue
            if (
                grid_x < 0
                or grid_y < 0
                or grid_x >= static_map.width
                or grid_y >= static_map.height
            ):
                _LOGGER.debug(
                    "Skipping native Narwal trail point outside map bounds: "
                    "%.1f, %.1f (%dx%d)",
                    grid_x,
                    grid_y,
                    static_map.width,
                    static_map.height,
                )
                continue
            point = (grid_x, grid_y)
            if points and math.hypot(
                point[0] - points[-1][0],
                point[1] - points[-1][1],
            ) < _NATIVE_TRAIL_MIN_GRID_DELTA:
                continue
            points.append(point)
            if len(points) >= _NATIVE_TRAIL_MAX_POINTS:
                break
        return points

    @staticmethod
    def _trajectory_cache_key(
        points: list[tuple[float, float]] | None,
    ) -> tuple[int, int] | tuple[()]:
        """Return a compact cache key for native trajectory changes."""
        if not points:
            return ()
        rounded = tuple((round(x, 2), round(y, 2)) for x, y in points)
        return (len(rounded), hash(rounded))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-render the map when new data arrives from the coordinator."""
        state = self.coordinator.client.state
        display = state.map_display_data

        static_map = state.map_data
        if not static_map or not static_map.compressed_map:
            self.async_write_ha_state()
            return
        if static_map.width <= 0 or static_map.height <= 0:
            self.async_write_ha_state()
            return

        static_key = self._static_map_key(static_map)
        if self._defer_static_map_while_remapping(state, static_key):
            self._clear_cached_map_image()
            self.async_write_ha_state()
            return

        native_trail: list[tuple[float, float]] | None = None
        if display and display.trajectory:
            native_trail = self._native_trajectory_grid_points(
                display.trajectory,
                static_map,
            )

        map_options_key = (
            self._map_option(CONF_SHOW_ROOM_LABELS),
            self._map_option(CONF_SHOW_FURNITURE),
            self._map_option(CONF_SHOW_FURNITURE_LABELS),
        )
        view_options_key = (self._map_rotation(), self._map_zoom())
        if display:
            cleaned_area_key = self._cleaned_area_key(display.cleaned_area)
            new_key = (
                static_key,
                map_options_key,
                view_options_key,
                display.robot_x,
                display.robot_y,
                display.robot_heading,
                self._trajectory_cache_key(native_trail),
                cleaned_area_key,
            )
        else:
            new_key = (static_key, map_options_key, view_options_key)

        now = time.monotonic()
        since_render = now - self._last_render_time if self._last_render_time else 999
        options_changed = (
            bool(self._cache_key)
            and (
                self._cache_key[1] != map_options_key
                or self._cache_key[2] != view_options_key
            )
        )

        if new_key == self._cache_key and self._cached_image:
            self.async_write_ha_state()
            return

        if (
            self._cached_image
            and since_render < _MIN_RENDER_INTERVAL
            and not options_changed
        ):
            self.async_write_ha_state()
            return

        self._schedule_render(display, new_key, native_trail)

    def _schedule_render(self, display, new_key, native_trail) -> None:
        """Schedule one render and coalesce updates that arrive while it runs."""
        if self._render_task is not None and not self._render_task.done():
            self._pending_render = (display, new_key, native_trail)
            return

        self._render_generation += 1
        generation = self._render_generation
        task = self.hass.async_create_task(
            self._async_render(display, new_key, generation, native_trail)
        )
        if task is not None:
            self._render_task = task
            task.add_done_callback(self._render_done)

    def _render_done(self, task: asyncio.Task) -> None:
        """Run the most recent pending render after the active render completes."""
        if self._render_task is task:
            self._render_task = None
        if self._pending_render is None:
            return
        display, new_key, native_trail = self._pending_render
        self._pending_render = None
        self._schedule_render(display, new_key, native_trail)

    async def _async_render(self, display, new_key, generation: int, native_trail) -> None:
        """Render the map image in an executor thread."""
        state = self.coordinator.client.state
        static_map = state.map_data
        if not static_map:
            self.async_write_ha_state()
            return

        from .narwal_client.map_renderer import (
            render_base_map,
            render_overlay,
            room_label_points,
        )

        # Rebuild base map only when static map data changes
        static_key = self._static_map_key(static_map)
        map_options_key = (
            self._map_option(CONF_SHOW_ROOM_LABELS),
            self._map_option(CONF_SHOW_FURNITURE),
            self._map_option(CONF_SHOW_FURNITURE_LABELS),
        )
        if (
            self._base_map_image is None
            or static_key != self._base_map_key
            or map_options_key != self._base_map_options_key
        ):
            room_names: dict[int, str] | None = None
            if map_options_key[0] and static_map.rooms:
                room_names = {
                    r.room_id: r.display_name for r in static_map.rooms
                }
            obstacles = static_map.obstacles if map_options_key[1] else None
            base_img = await self.hass.async_add_executor_job(
                render_base_map,
                static_map.compressed_map,
                static_map.width,
                static_map.height,
                None,
                None,
                room_names,
                obstacles,
                static_map.origin_x,
                static_map.origin_y,
                map_options_key[2],
                False,
                False,
            )
            if base_img:
                if generation != self._render_generation:
                    return
                room_points = await self.hass.async_add_executor_job(
                    room_label_points,
                    static_map.compressed_map,
                    static_map.width,
                    static_map.height,
                    room_names,
                )
                if generation != self._render_generation:
                    return
                self._base_map_image = base_img
                self._base_map_key = static_key
                self._base_map_options_key = map_options_key
                self._room_label_points = room_points
                _LOGGER.info(
                    "Base map rendered (key=%s, %dx%d)",
                    static_key,
                    static_map.width,
                    static_map.height,
                )
            else:
                self.async_write_ha_state()
                return

        # Compute robot grid position
        robot_x = None
        robot_y = None
        robot_heading = None
        if display:
            grid_pos = display.to_grid_coords(
                static_map.resolution, static_map.origin_x, static_map.origin_y,
            )
            if grid_pos is not None:
                robot_x, robot_y = grid_pos
                robot_heading = display.robot_heading
                # Log transform details periodically for debugging position offset
                if self._render_count % 30 == 0:
                    try:
                        # Compare display_map dock ref (field 5) with static map dock
                        dock_ref_grid_x = dock_ref_grid_y = None
                        if display.dock_ref_x != 0.0 or display.dock_ref_y != 0.0:
                            dock_ref_grid_x = display.dock_ref_x - static_map.origin_x
                            dock_ref_grid_y = display.dock_ref_y - static_map.origin_y
                        # Room lookup at robot grid position
                        from .narwal_client.map_renderer import lookup_room_at_grid
                        robot_rid, robot_room = lookup_room_at_grid(
                            static_map.compressed_map, static_map.width, static_map.height,
                            int(robot_x), int(robot_y),
                        )
                        dock_rid, dock_room = (-1, "n/a")
                        if static_map.dock_x is not None and static_map.dock_y is not None:
                            dock_rid, dock_room = lookup_room_at_grid(
                                static_map.compressed_map, static_map.width, static_map.height,
                                int(static_map.dock_x), int(static_map.dock_y),
                        )
                        _LOGGER.debug(
                            "POSITION DIAG: robot_raw=(%.2f, %.2f) "
                            "robot_grid=(%.1f, %.1f) robot_room=%s(id=%d) "
                            "| dock_ref_raw=(%.2f, %.2f) dock_ref_grid=(%.1f, %.1f) "
                            "| static_dock_grid=(%.1f, %.1f) dock_room=%s(id=%d) "
                            "| res=%d origin=(%d, %d) map=%dx%d",
                            display.robot_x, display.robot_y,
                            robot_x, robot_y, robot_room, robot_rid,
                            display.dock_ref_x, display.dock_ref_y,
                            dock_ref_grid_x or 0, dock_ref_grid_y or 0,
                            static_map.dock_x or 0, static_map.dock_y or 0,
                            dock_room, dock_rid,
                            static_map.resolution,
                            static_map.origin_x, static_map.origin_y,
                            static_map.width, static_map.height,
                        )
                    except Exception:
                        _LOGGER.debug("POSITION DIAG failed", exc_info=True)

        cleaned_area = display.cleaned_area if display else None

        try:
            png_bytes = await self.hass.async_add_executor_job(
                render_overlay,
                self._base_map_image,
                static_map.height,
                robot_x,
                robot_y,
                robot_heading,
                native_trail,
                self._map_rotation(),
                self._map_zoom(),
                self._room_label_points,
                static_map.dock_x,
                static_map.dock_y,
                cleaned_area,
            )

            if png_bytes:
                if generation != self._render_generation:
                    return
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = time.monotonic()
                self._render_count += 1

        except Exception:
            _LOGGER.exception("Failed to render map overlay")

        self.async_write_ha_state()
