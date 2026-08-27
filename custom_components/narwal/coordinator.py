"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, NO_BROADCAST_PRODUCT_KEYS
from .dock_tasks import can_start_robot_clean, dock_task_blocks_robot_return
from .narwal_client import (
    CleaningRoute,
    CommandResponse,
    FanLevel,
    MapDisplayData,
    MopHumidity,
    MopStrengthLevel,
    NarwalClient,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkMode,
)
from .narwal_client.const import ACTIVE_CLEANING_STATUSES, WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal

# Consumable alerts change over weeks — poll every ~30 min (30 * POLL_INTERVAL).
CONSUMABLE_POLL_EVERY = 30
MAP_DISPLAY_CACHE_VERSION = 1
MAP_DISPLAY_CACHE_SAVE_INTERVAL = 10.0

# The robot only broadcasts working_status and display_map while an
# active_robot_publish subscription is live, and that subscription lasts
# TOPIC_SUBSCRIPTION_TTL seconds. Renew well inside the window: once it lapses the
# robot goes quiet on both topics, the vacuum entity freezes on its last
# base_status-derived value, and the live map stops updating (#73).
TOPIC_SUBSCRIPTION_TTL = 600.0
TOPIC_RESUBSCRIBE_AFTER = 240.0
ROOM_CLEAN_SETTING_ATTRS = frozenset(field.name for field in fields(RoomCleanSettings))
MOP_WORK_MODES = frozenset(
    {WorkMode.MOP, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)
VACUUM_WORK_MODES = frozenset(
    {WorkMode.VACUUM, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)


@dataclass(frozen=True)
class _MapDisplayCacheSnapshot:
    """Lightweight display-map trajectory snapshot queued for persistence."""

    map_id: int
    map_created_at: int
    active_clean: bool
    display: MapDisplayData

    @property
    def trajectory_signature(self) -> tuple[int, int, int] | tuple[()]:
        """Return the native trajectory signature for this snapshot."""
        return self.display.trajectory_signature


def _status_payload(response: CommandResponse) -> dict[str, object] | None:
    """Return the decoded robot_base_status payload from a response."""
    if not response.accepted or not isinstance(response.data, dict):
        return None
    status_data = response.data.get("2")
    if isinstance(status_data, dict) and status_data:
        return status_data
    return None


def _has_dock_status_payload(response: CommandResponse) -> bool:
    """Return True when a response carries the dock status submessage."""
    status_data = _status_payload(response)
    if status_data is None:
        return False
    field3 = status_data.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict):
        return False
    return bool({"1", "2", "3", "7", "10", "12", "18"}.intersection(field3))


@dataclass
class CleanSettings(RoomCleanSettings):
    """User-selected clean parameters for the next clean or live controls.

    Select/number entities mutate this, and clean-start paths read it. Each
    entity persists its value via RestoreEntity, so settings survive restarts.
    Only fan and water have live setters; the other parameters take effect at
    the next start.
    """

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1
    route: CleaningRoute = CleaningRoute.METICULOUS


def _state_attr_is_true(state: NarwalState, attr: str) -> bool:
    """Return True only for explicit boolean state properties."""
    return getattr(state, attr, False) is True


def has_blocking_error(state: NarwalState | None) -> bool:
    """Return True when the robot reports a command-blocking error."""
    return (
        state is None
        or state.working_status == WorkingStatus.ERROR
        or _state_attr_is_true(state, "has_error")
    )


def is_active_clean_session(state: NarwalState | None) -> bool:
    """Return True while clean parameters are locked to the current task."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
    ) and not _state_attr_is_true(state, "is_returning")


def is_clean_session_context(state: NarwalState | None) -> bool:
    """Return True while robot-side clean task context is still current."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
        or _state_attr_is_true(state, "is_returning")
    )


def is_live_clean_setting_available(state: NarwalState | None) -> bool:
    """Return True when live clean settings can be changed during a task."""
    if has_blocking_error(state):
        return False
    return (
        (_state_attr_is_true(state, "is_cleaning") or is_active_clean_session(state))
        and not dock_task_blocks_robot_return(state)
    )


def clean_setting_applies_to_mode(attr: str, work_mode: WorkMode | None) -> bool:
    """Return True when a clean setting is meaningful for the selected mode."""
    if work_mode is None:
        return True
    if attr in {"water", "mop_strength"}:
        return work_mode in MOP_WORK_MODES
    if attr == "fan":
        return work_mode in VACUUM_WORK_MODES
    return True


def is_narwal_task_busy(state: NarwalState | None) -> bool:
    """Return True while the robot or dock is busy with a task phase."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
        or _state_attr_is_true(state, "is_returning")
        or _state_attr_is_true(state, "is_charging_to_resume")
        or (
            _state_attr_is_true(state, "is_station_active")
            and _state_attr_is_true(state, "blocks_robot_start_for_dock_task")
        )
    )


def can_edit_pending_clean_settings(state: NarwalState | None) -> bool:
    """Return True when pending next-clean settings can be edited locally."""
    if state is None:
        return True
    if has_blocking_error(state):
        return False
    return not is_narwal_task_busy(state)


def can_start_cleaning(state: NarwalState | None) -> bool:
    """Return True when a new robot clean command can be sent now."""
    if has_blocking_error(state):
        return False
    return (
        state.is_docked
        and not is_clean_session_context(state)
        and can_start_robot_clean(state)
    )


def can_pause_cleaning(state: NarwalState | None) -> bool:
    """Return True when the active robot clean can be paused."""
    if has_blocking_error(state):
        return False
    return (
        (
            _state_attr_is_true(state, "is_cleaning")
            or state.working_status == WorkingStatus.REMAPPING
        )
        and not _state_attr_is_true(state, "is_paused")
        and not dock_task_blocks_robot_return(state)
    )


def can_resume_cleaning(state: NarwalState | None) -> bool:
    """Return True when a paused robot clean can be resumed."""
    if has_blocking_error(state):
        return False
    return (
        (
            state.working_status in (*ACTIVE_CLEANING_STATUSES, WorkingStatus.REMAPPING)
            or _state_attr_is_true(state, "has_paused_clean_task_context")
        )
        and _state_attr_is_true(state, "is_paused")
        and not dock_task_blocks_robot_return(state)
    )


def can_stop_cleaning(state: NarwalState | None) -> bool:
    """Return True when a robot-side clean task can be stopped."""
    if has_blocking_error(state):
        return False
    return is_clean_session_context(state)


def can_return_home(state: NarwalState | None) -> bool:
    """Return True when the robot can be recalled to the dock."""
    if has_blocking_error(state):
        return False
    return (
        not state.is_docked
        and state.working_status != WorkingStatus.TASK_COMPLETED
        and not _state_attr_is_true(state, "is_returning")
        and not dock_task_blocks_robot_return(state)
    )


def can_locate_robot(state: NarwalState | None) -> bool:
    """Return True when the locate command can be sent."""
    if has_blocking_error(state):
        return False
    return not dock_task_blocks_robot_return(state)


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Setup is kept fast: connect, try a few commands (which may time out if
    the robot is asleep), then start the listener. The listener's keepalive
    loop handles waking the robot — no blocking wake call during setup.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        product_key = entry.data.get("product_key")
        topic_prefix = f"/{product_key}" if product_key else None
        supports_broadcasts = product_key not in NO_BROADCAST_PRODUCT_KEYS
        self.client = NarwalClient(
            host=entry.data["host"],
            port=entry.data["port"],
            device_id=entry.data.get("device_id", ""),
            topic_prefix=topic_prefix,
            supports_broadcasts=supports_broadcasts,
        )
        self.clean_settings = CleanSettings()
        self.room_clean_settings: dict[tuple[str | None, int], RoomCleanSettings] = {}
        self.room_clean_settings_customized: dict[tuple[str | None, int], set[str]] = {}
        self.active_clean_work_mode: WorkMode | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        self._last_display_map_resub: float = 0.0
        self._last_topic_subscribe: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable
        self._dock_status_refresh_failed = True
        self._consumable_poll_countdown = 0
        self._map_display_cache_store = Store(
            hass,
            MAP_DISPLAY_CACHE_VERSION,
            f"{DOMAIN}_map_display_{entry.entry_id}",
        )
        self._map_display_cache_signature: tuple[int, int, int] | tuple[()] = ()
        self._map_display_cache_last_save = 0.0
        self._pending_map_display_cache_snapshot: _MapDisplayCacheSnapshot | None = None
        self._pending_map_display_cache_restore: dict[str, object] | None = None
        self._map_display_cache_save_task: asyncio.Task[None] | None = None
        self._map_display_cache_restored = False
        self._map_display_cache_restored_from_active = False
        self.dock_action_lock = asyncio.Lock()

    @property
    def has_fresh_state(self) -> bool:
        """Return true when the coordinator has not returned stale poll data."""
        return self.last_update_success and not self._dock_status_refresh_failed

    def _mark_dock_status_refresh_failed(self) -> None:
        """Record that dock-control state may be stale."""
        self._dock_status_refresh_failed = True

    def _mark_dock_status_refresh_succeeded(self) -> None:
        """Record that dock-control state came from a current base-status payload."""
        self._dock_status_refresh_failed = False

    def default_room_clean_settings(self) -> RoomCleanSettings:
        """Return a room-clean profile copied from the current global defaults."""
        return RoomCleanSettings(
            work_mode=self.clean_settings.work_mode,
            fan=self.clean_settings.fan,
            water=self.clean_settings.water,
            mop_strength=self.clean_settings.mop_strength,
            passes=self.clean_settings.passes,
            route=self.clean_settings.route,
        )

    @staticmethod
    def compatible_room_clean_work_mode(
        room_settings: Mapping[int, RoomCleanSettings],
    ) -> WorkMode:
        """Return the shared work mode, rejecting mixed-mode room profiles."""
        modes = {settings.work_mode for settings in room_settings.values()}
        if len(modes) == 1:
            return next(iter(modes))
        raise ValueError("Mixed Narwal room clean modes are not supported")

    def record_accepted_clean_start(
        self,
        room_settings: Mapping[int, RoomCleanSettings],
    ) -> None:
        """Record the effective mode for the accepted robot task."""
        self.active_clean_work_mode = self.compatible_room_clean_work_mode(
            room_settings
        )

    def clean_setting_applicability_mode(
        self, *, live: bool = False
    ) -> WorkMode | None:
        """Return the mode used to decide whether fan/water controls apply."""
        if live:
            state = self.data or self.client.state
            if is_clean_session_context(state):
                return self.active_clean_work_mode
        return self.clean_settings.work_mode

    def _sync_active_clean_context(self, state: NarwalState) -> None:
        """Clear accepted-task metadata once the robot is no longer in a clean context."""
        if not is_clean_session_context(state):
            self.active_clean_work_mode = None

    @staticmethod
    def _normalise_room_settings_map_id(map_id: object) -> str | None:
        """Return a stable map id for room profiles."""
        if map_id in (None, "", 0, "0"):
            return None
        return str(map_id)

    def room_settings_map_id(self, map_data: object | None = None) -> str | None:
        """Return the active map id used to scope room profiles."""
        if map_data is None:
            state = self.data or self.client.state
            map_data = getattr(state, "map_data", None) if state is not None else None
        return self._normalise_room_settings_map_id(getattr(map_data, "map_id", None))

    def _room_clean_settings_key(
        self,
        room_id: int,
        map_id: str | None = None,
    ) -> tuple[str | None, int]:
        """Return the storage key for a room profile."""
        return (map_id if map_id is not None else self.room_settings_map_id(), room_id)

    def room_clean_settings_for(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        """Return the configured room-clean profile for a room."""
        key = self._room_clean_settings_key(room_id, map_id)
        if key not in self.room_clean_settings:
            self.room_clean_settings[key] = self.default_room_clean_settings()
        return self.room_clean_settings[key]

    def effective_room_clean_settings_for(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        """Return the room profile after applying current global fallbacks."""
        return self.room_clean_settings_for_rooms([room_id], map_id=map_id)[room_id]

    def room_clean_settings_for_rooms(
        self,
        room_ids: list[int],
        *,
        default: RoomCleanSettings | None = None,
        map_id: str | None = None,
        use_room_profiles: bool = True,
    ) -> dict[int, RoomCleanSettings]:
        """Return stored room-clean profiles for a set of rooms.

        Missing rooms use the supplied default without creating profile entries.
        """
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        fallback = default or self.default_room_clean_settings()
        if not use_room_profiles:
            return {room_id: fallback for room_id in room_ids}
        customized = getattr(self, "room_clean_settings_customized", {})
        settings: dict[int, RoomCleanSettings] = {}
        for room_id in room_ids:
            key = (map_key, room_id)
            profile = self.room_clean_settings.get(key)
            custom_fields = customized.get(key, set())
            if profile is None or not custom_fields:
                settings[room_id] = fallback
                continue
            merged = RoomCleanSettings(
                work_mode=fallback.work_mode,
                fan=fallback.fan,
                water=fallback.water,
                mop_strength=fallback.mop_strength,
                passes=fallback.passes,
                route=fallback.route,
            )
            for attr in custom_fields:
                if attr in ROOM_CLEAN_SETTING_ATTRS:
                    setattr(merged, attr, getattr(profile, attr))
            settings[room_id] = merged
        return settings

    def set_room_clean_setting(
        self,
        room_id: int,
        attr: str,
        value,
        *,
        map_id: str | None = None,
    ) -> None:
        """Store one room-clean profile value."""
        if attr not in ROOM_CLEAN_SETTING_ATTRS:
            raise AttributeError(f"Unsupported room clean setting: {attr}")
        key = self._room_clean_settings_key(room_id, map_id)
        setattr(self.room_clean_settings_for(room_id, map_id=map_id), attr, value)
        if not hasattr(self, "room_clean_settings_customized"):
            self.room_clean_settings_customized = {}
        self.room_clean_settings_customized.setdefault(key, set()).add(attr)

    def clear_room_clean_setting(
        self,
        room_id: int,
        attr: str,
        *,
        map_id: str | None = None,
    ) -> None:
        """Clear one room-clean override so the room inherits the global value."""
        if attr not in ROOM_CLEAN_SETTING_ATTRS:
            raise AttributeError(f"Unsupported room clean setting: {attr}")
        key = self._room_clean_settings_key(room_id, map_id)
        customized = self.room_clean_settings_customized.get(key)
        if not customized:
            return
        customized.discard(attr)
        if customized:
            return
        self.room_clean_settings_customized.pop(key, None)
        self.room_clean_settings.pop(key, None)

    def _map_display_cache_payload(
        self,
        state: NarwalState,
    ) -> dict[str, object] | None:
        """Return a serializable display-map trajectory cache payload."""
        snapshot = self._map_display_cache_snapshot(state)
        return (
            self._map_display_cache_payload_from_snapshot(snapshot)
            if snapshot is not None
            else None
        )

    @staticmethod
    def _map_display_cache_snapshot(
        state: NarwalState,
    ) -> _MapDisplayCacheSnapshot | None:
        """Return a lightweight display-map trajectory cache snapshot."""
        display = state.map_display_data
        if display is None or not display.has_trajectory:
            return None
        static_map = state.map_data
        return _MapDisplayCacheSnapshot(
            map_id=getattr(static_map, "map_id", 0) if static_map else 0,
            map_created_at=getattr(static_map, "created_at", 0) if static_map else 0,
            active_clean=is_active_clean_session(state),
            display=display,
        )

    @staticmethod
    def _map_display_cache_payload_from_snapshot(
        snapshot: _MapDisplayCacheSnapshot,
    ) -> dict[str, object]:
        """Return a serializable display-map trajectory cache payload."""
        display = snapshot.display
        return {
            "map_id": snapshot.map_id,
            "map_created_at": snapshot.map_created_at,
            "active_clean": snapshot.active_clean,
            "robot_x": display.robot_x,
            "robot_y": display.robot_y,
            "robot_heading": display.robot_heading,
            "timestamp": display.timestamp,
            "dock_ref_x": display.dock_ref_x,
            "dock_ref_y": display.dock_ref_y,
            "trajectory_x_values": base64.b64encode(
                display.trajectory_x_values
            ).decode("ascii"),
            "trajectory_y_values": base64.b64encode(
                display.trajectory_y_values
            ).decode("ascii"),
            "trajectory_signature": list(display.trajectory_signature),
        }

    @staticmethod
    def _optional_cache_int(value: object) -> int | None:
        """Return an integer cache value, treating blank/zero as absent."""
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _map_display_from_cache(
        payload: Mapping[str, object] | None,
    ) -> MapDisplayData | None:
        """Return cached display-map data, if the stored payload is valid."""
        if not payload:
            return None
        try:
            trajectory_x_values = base64.b64decode(
                str(payload["trajectory_x_values"])
            )
            trajectory_y_values = base64.b64decode(
                str(payload["trajectory_y_values"])
            )
            signature_raw = payload["trajectory_signature"]
            if not isinstance(signature_raw, list):
                return None
            signature = tuple(int(value) for value in signature_raw)
            if len(signature) != 3:
                return None
            display = MapDisplayData(
                # Cached trajectories are persisted so completed routes survive
                # restart, but robot pose must come from a live display_map packet.
                robot_x=0.0,
                robot_y=0.0,
                robot_heading=0.0,
                timestamp=int(payload.get("timestamp", 0)),
                dock_ref_x=float(payload.get("dock_ref_x", 0.0)),
                dock_ref_y=float(payload.get("dock_ref_y", 0.0)),
                trajectory_x_values=trajectory_x_values,
                trajectory_y_values=trajectory_y_values,
                trajectory_signature=signature,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return display if display.has_trajectory else None

    async def _async_restore_map_display_cache(self) -> None:
        """Restore the last display-map trajectory for the active static map."""
        payload = await self._map_display_cache_store.async_load()
        if not isinstance(payload, Mapping):
            return
        if self._has_current_map_display_trajectory():
            return
        if self.client.state.map_data is None:
            self._pending_map_display_cache_restore = dict(payload)
            return
        self._restore_map_display_cache_payload(payload)

    def _restore_pending_map_display_cache(self) -> None:
        """Restore a delayed display-map cache once a static map is available."""
        payload = self._pending_map_display_cache_restore
        if payload is None:
            return
        self._pending_map_display_cache_restore = None
        if self._has_current_map_display_trajectory():
            return
        self._restore_map_display_cache_payload(payload)

    def _restore_map_display_cache_payload(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Restore a display-map cache payload if it matches the active map."""
        display = self._map_display_from_cache(payload)
        if display is None:
            return False

        static_map = self.client.state.map_data
        if static_map is None:
            self._pending_map_display_cache_restore = dict(payload)
            return False
        cached_map_id = self._optional_cache_int(payload.get("map_id"))
        cached_created_at = self._optional_cache_int(payload.get("map_created_at"))
        if cached_map_id is not None and cached_map_id != static_map.map_id:
            return False
        if (
            cached_created_at is not None
            and cached_created_at != static_map.created_at
        ):
            return False
        if self._has_current_map_display_trajectory():
            return False

        self.client.state.map_display_data = display
        self._map_display_cache_signature = display.trajectory_signature
        self._map_display_cache_restored = True
        self._map_display_cache_restored_from_active = bool(
            payload.get("active_clean")
        )
        _LOGGER.debug(
            "Restored Narwal display-map trajectory cache with %d bytes",
            len(display.trajectory_x_values) + len(display.trajectory_y_values),
        )
        return True

    def _reset_map_display_cache_state(self, *, clear_memory: bool) -> None:
        """Reset in-memory display-map trail cache state."""
        self._pending_map_display_cache_snapshot = None
        self._pending_map_display_cache_restore = None
        self._map_display_cache_signature = ()
        self._map_display_cache_restored = False
        self._map_display_cache_restored_from_active = False
        if clear_memory:
            self.client.state.map_display_data = None

    def _has_current_map_display_trajectory(self) -> bool:
        """Return true when current state already has native trajectory data."""
        display = self.client.state.map_display_data
        return display is not None and display.has_trajectory

    @staticmethod
    def _map_display_signature_from_payload(
        payload: Mapping[str, object],
    ) -> tuple[int, int, int] | tuple[()]:
        """Return the trajectory signature stored in a cache payload."""
        signature_raw = payload.get("trajectory_signature")
        return (
            tuple(int(value) for value in signature_raw)
            if isinstance(signature_raw, list)
            else ()
        )

    def _schedule_map_display_cache_save(
        self, state: NarwalState, *, immediate: bool = False
    ) -> None:
        """Schedule a throttled save of the latest display-map trajectory."""
        snapshot = self._map_display_cache_snapshot(state)
        if snapshot is None:
            return
        if snapshot.trajectory_signature == self._map_display_cache_signature:
            return
        self._pending_map_display_cache_snapshot = snapshot
        if (
            self._map_display_cache_save_task is not None
            and not self._map_display_cache_save_task.done()
        ):
            return
        delay = (
            0.0
            if immediate
            else max(
                0.0,
                MAP_DISPLAY_CACHE_SAVE_INTERVAL
                - (time.monotonic() - self._map_display_cache_last_save),
            )
        )
        self._map_display_cache_save_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_save_pending_map_display_cache(delay),
            f"{DOMAIN}_map_display_cache_save",
        )

    def _cancel_map_display_cache_save_task(self) -> asyncio.Task[None] | None:
        """Cancel any queued trajectory-cache write and return the task."""
        task = self._map_display_cache_save_task
        if task is None or task.done():
            return None
        task.cancel()
        return task

    async def _async_cancel_map_display_cache_save(self) -> None:
        """Cancel and await any queued trajectory-cache write."""
        task = self._cancel_map_display_cache_save_task()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._map_display_cache_save_task is task:
            self._map_display_cache_save_task = None

    async def _async_save_pending_map_display_cache(self, delay: float) -> None:
        """Persist the newest queued display-map trajectory cache payload."""
        if delay > 0:
            await asyncio.sleep(delay)
        while self._pending_map_display_cache_snapshot is not None:
            snapshot = self._pending_map_display_cache_snapshot
            self._pending_map_display_cache_snapshot = None
            payload = self._map_display_cache_payload_from_snapshot(snapshot)
            try:
                await self._map_display_cache_store.async_save(payload)
            except Exception:
                _LOGGER.debug("Could not save display-map trajectory cache")
                return
            self._map_display_cache_signature = snapshot.trajectory_signature
            self._map_display_cache_last_save = time.monotonic()
            if self._pending_map_display_cache_snapshot is not None:
                await asyncio.sleep(MAP_DISPLAY_CACHE_SAVE_INTERVAL)

    async def _async_flush_map_display_cache(self) -> None:
        """Persist the current display-map trajectory before shutdown."""
        await self._async_cancel_map_display_cache_save()

        snapshot = (
            self._pending_map_display_cache_snapshot
            or self._map_display_cache_snapshot(self.client.state)
        )
        self._pending_map_display_cache_snapshot = None
        payload = (
            self._map_display_cache_payload_from_snapshot(snapshot)
            if snapshot is not None
            else None
        )
        if payload is None:
            return

        try:
            await self._map_display_cache_store.async_save(payload)
        except Exception:
            _LOGGER.debug("Could not flush display-map trajectory cache")
            return
        self._map_display_cache_signature = self._map_display_signature_from_payload(
            payload
        )
        self._map_display_cache_last_save = time.monotonic()

    async def async_clear_map_display_cache(self) -> None:
        """Clear cached display-map trajectory after accepting a new clean."""
        await self._async_cancel_map_display_cache_save()
        self._reset_map_display_cache_state(clear_memory=True)
        with contextlib.suppress(Exception):
            await self._map_display_cache_store.async_save({})

    def _schedule_map_display_cache_clear(
        self,
        snapshot: _MapDisplayCacheSnapshot | None,
    ) -> None:
        """Clear or replace persisted trail cache from a synchronous callback."""
        self._cancel_map_display_cache_save_task()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_clear_or_replace_map_display_cache(snapshot),
            f"{DOMAIN}_map_display_cache_clear",
        )

    async def _async_clear_or_replace_map_display_cache(
        self,
        snapshot: _MapDisplayCacheSnapshot | None,
    ) -> None:
        """Persist the new-clean cache decision after cancelling stale writes."""
        await self._async_cancel_map_display_cache_save()
        self._pending_map_display_cache_snapshot = None
        if snapshot is None:
            with contextlib.suppress(Exception):
                await self._map_display_cache_store.async_save({})
            return
        payload = self._map_display_cache_payload_from_snapshot(snapshot)
        try:
            await self._map_display_cache_store.async_save(payload)
        except Exception:
            _LOGGER.debug("Could not replace display-map trajectory cache")
            return
        self._map_display_cache_signature = snapshot.trajectory_signature
        self._map_display_cache_last_save = time.monotonic()

    def _is_new_clean_transition(self, state: NarwalState) -> bool:
        """Return true when state has entered a new robot cleaning session."""
        if not (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or (
                state.has_recent_active_working_status
                and not _state_attr_is_true(state, "is_returning")
            )
        ):
            return False
        if self._prev_working_status in ACTIVE_CLEANING_STATUSES:
            return False
        return not (
            self._prev_working_status == WorkingStatus.UNKNOWN
            and self._map_display_cache_restored
            and self._map_display_cache_restored_from_active
        )

    def _clear_map_display_cache_for_new_clean(self, state: NarwalState) -> None:
        """Clear stale trail data when a clean starts outside HA."""
        keep_fresh_display = (
            state.map_display_data is not None
            and state.map_display_data.has_trajectory
            and self.client.last_display_map_age <= 5.0
        )
        if keep_fresh_display:
            snapshot = self._map_display_cache_snapshot(state)
            self._reset_map_display_cache_state(clear_memory=False)
            self._pending_map_display_cache_snapshot = snapshot
            self._schedule_map_display_cache_clear(snapshot)
            _LOGGER.debug(
                "Replaced Narwal display-map trajectory cache for new clean"
            )
            return
        self._reset_map_display_cache_state(clear_memory=not keep_fresh_display)
        self._schedule_map_display_cache_clear(None)
        _LOGGER.debug(
            "Cleared Narwal display-map trajectory cache for new clean%s",
            " while keeping fresh display_map data" if keep_fresh_display else "",
        )

    def _handle_working_status_transition(self, state: NarwalState) -> None:
        """Apply transition side effects and record the latest working status."""
        if self._is_new_clean_transition(state):
            self._clear_map_display_cache_for_new_clean(state)
        self._prev_working_status = state.working_status

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Queries initial state BEFORE starting the listener to avoid
        concurrent recv issues (see 446be16). Each command is wrapped in
        try/except so setup never crashes if the robot is asleep.
        The listener's keepalive loop handles waking independently.
        """
        await self.client.connect()

        # Fetch initial state BEFORE starting listener (no concurrent recv)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info at startup")

        try:
            response = await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if not _has_dock_status_payload(response):
                raise NarwalConnectionError(
                    "Status refresh returned no dock-status payload"
                )
            self._mark_dock_status_refresh_succeeded()
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Could not fetch initial status")

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        try:
            await self._async_restore_map_display_cache()
        except Exception:
            _LOGGER.debug("Could not restore display-map trajectory cache")

        try:
            await self.client.get_consumable_info()
        except Exception:
            _LOGGER.debug("Could not fetch initial consumable info")

        # Subscribe to broadcast topics (display_map, working_status, etc.)
        # Must be sent before listener starts so display_map flows during cleaning.
        if self.client.supports_broadcasts:
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
            except Exception:
                _LOGGER.debug("Could not send topic subscription at startup")

        self.async_set_updated_data(self.client.state)
        self._prev_working_status = self.client.state.working_status

        # Set up push callback and start persistent listener
        self.client.on_state_update = self._on_state_update
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
        )

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name, state.battery_level,
            state.is_docked, self.client.robot_awake,
        )

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle a push state update from the WebSocket listener."""
        # Push data arriving means robot is reachable — reset failure counter
        self._consecutive_failures = 0

        # Fetch static map if missing (get_map failed at startup)
        if state.map_data is None and not self._map_fetch_pending:
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_missing_map(),
                f"{DOMAIN}_map_fetch",
            )
        elif state.map_data is not None:
            self._restore_pending_map_display_cache()

        # Detect return-to-dock transition: CLEANING/CLEANING_ALT → docked state.
        # Broadcast dock fields are stale after docking — immediate poll
        # refreshes them so UI shows DOCKED instead of IDLE.
        # On older FW the transition is → STANDBY; on v01.07.23+ it may
        # go directly to DOCKED_V2(2).
        if (
            state.working_status in (
                WorkingStatus.STANDBY, WorkingStatus.DOCKED_V2,
            )
            and self._prev_working_status
            in ACTIVE_CLEANING_STATUSES
        ):
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._handle_working_status_transition(state)
        self._schedule_map_display_cache_save(state)

        # display_map dropout recovery: if cleaning but no display_map for
        # 30s, re-send topic subscription. Only subscription — no wake burst
        # (wake bursts during cleaning cause pause bouncing).
        is_cleaning = (
            state.is_cleaning
            or state.has_recent_active_working_status
            or (
                not state.is_docked
                and state.working_status in ACTIVE_CLEANING_STATUSES
            )
        )
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            if (
                display_age > 30.0
                and now - self._last_display_map_resub > 45.0
            ):
                _LOGGER.info(
                    "display_map dropout (%.0fs) — re-subscribing to topics",
                    display_age,
                )
                self._last_display_map_resub = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._resub_topics(),
                    f"{DOMAIN}_resub",
                )

        self._sync_active_clean_context(state)
        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Broadcast received (status=%s) — normal polling restored",
                state.working_status.name,
            )

    async def _fetch_missing_map(self) -> None:
        """Fetch static map when it's missing (get_map failed at startup)."""
        try:
            await self.client.get_map()
            _LOGGER.info("Static map loaded (was missing at startup)")
        except Exception:
            _LOGGER.debug("Map fetch failed — will retry on next broadcast")
            self._map_fetch_pending = False
            return
        self._restore_pending_map_display_cache()
        if self.client.supports_broadcasts:
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
            except Exception:
                _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        if not self.client.supports_broadcasts:
            return
        try:
            await self.client.subscribe_to_topics()
            self._last_topic_subscribe = time.monotonic()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            response = await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if not _has_dock_status_payload(response):
                raise NarwalConnectionError(
                    "Status refresh returned no dock-status payload"
                )
            self._mark_dock_status_refresh_succeeded()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status after transition")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)

    async def async_refresh_dock_status(self) -> bool:
        """Refresh full dock/base-station status for action gating."""
        full_update = not self.client.state.has_recent_active_working_status
        try:
            response = await self.client.get_status(full_update=full_update)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if not response.accepted:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug(
                "Dock status refresh was rejected with code %s",
                response.result_code,
            )
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update and not _has_dock_status_payload(response):
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Dock status refresh returned no dock-status payload")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update:
            self._mark_dock_status_refresh_succeeded()
        self._sync_active_clean_context(self.client.state)
        self.async_set_updated_data(self.client.state)
        return True

    async def async_refresh_action_status(self) -> bool:
        """Refresh state for a robot action without clobbering live task telemetry."""
        full_update = not self.client.state.has_recent_active_working_status
        try:
            response = await self.client.get_status(full_update=full_update)
        except Exception:
            _LOGGER.debug("Failed to refresh Narwal action status")
            if full_update:
                self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if not response.accepted:
            _LOGGER.debug(
                "Narwal action status refresh was rejected with code %s",
                response.result_code,
            )
            if full_update:
                self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update and not _has_dock_status_payload(response):
            _LOGGER.debug("Narwal action status refresh returned no dock-status payload")
            self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update:
            self._mark_dock_status_refresh_succeeded()
        self._sync_active_clean_context(self.client.state)
        self.async_set_updated_data(self.client.state)
        return True

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived.

        Reconnection is handled by the listener loop's exponential backoff.
        We do NOT call client.connect() here to avoid racing with the listener
        and violating the single-WS-connection-per-IP constraint.

        On poll failure, returns stale data for up to _max_failures consecutive
        failures (~5 minutes) before raising UpdateFailed.
        """
        try:
            if not self.client.connected:
                raise NarwalConnectionError("Not connected")
            full_update = not self.client.state.has_recent_active_working_status
            response = await self.client.get_status(full_update=full_update)
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if full_update and not _has_dock_status_payload(response):
                raise NarwalConnectionError(
                    "Status refresh returned no dock-status payload"
                )
        except Exception as err:
            self._consecutive_failures += 1
            self._mark_dock_status_refresh_failed()
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures, self._max_failures, err,
            )
            return self.client.state  # stale data keeps entities available
        else:
            self._consecutive_failures = 0
            if full_update:
                self._mark_dock_status_refresh_succeeded()

        self._handle_working_status_transition(self.client.state)

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            with contextlib.suppress(Exception):
                await self.client.get_map()

        # Renew the broadcast subscription before it lapses. This is deliberately
        # NOT conditional on believing we are cleaning: working_status is what tells
        # us we are cleaning, so gating renewal on that state deadlocks — the
        # subscription expires, the robot goes quiet, the entity stays "docked", and
        # nothing ever re-subscribes (#73).
        if (
            self.client.supports_broadcasts
            and time.monotonic() - self._last_topic_subscribe > TOPIC_RESUBSCRIBE_AFTER
        ):
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
                _LOGGER.debug("Renewed topic subscription")
            except Exception:
                _LOGGER.debug("Topic subscription renewal failed")

        # Refresh consumable alerts periodically (slow-changing; not broadcast)
        if self._consumable_poll_countdown <= 0:
            self._consumable_poll_countdown = CONSUMABLE_POLL_EVERY
            try:
                await self.client.get_consumable_info()
            except Exception:
                _LOGGER.debug("Consumable info poll failed")
        else:
            self._consumable_poll_countdown -= 1

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        self._sync_active_clean_context(self.client.state)
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task
        await self._async_flush_map_display_cache()
        await super().async_shutdown()
