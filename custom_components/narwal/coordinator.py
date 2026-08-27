"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, NO_BROADCAST_PRODUCT_KEYS
from .narwal_client import (
    CleaningRoute,
    CommandResponse,
    FanLevel,
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
        or _state_attr_is_true(state, "is_charging_to_resume")
    )


def is_live_clean_setting_available(state: NarwalState | None) -> bool:
    """Return True when live clean settings can be changed during a task."""
    if state is None:
        return False
    return (
        (_state_attr_is_true(state, "is_cleaning") or is_active_clean_session(state))
        and not _state_attr_is_true(state, "is_charging_to_resume")
        and not _state_attr_is_true(state, "is_station_active")
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
        and not state.blocks_robot_start_for_dock_task
    )


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
        self.selected_clean_rooms: dict[str | None, set[int]] = {}
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

    def selected_clean_room_ids_for(
        self,
        room_ids: list[int],
        *,
        map_id: str | None = None,
    ) -> list[int]:
        """Return selected clean rooms, falling back to all supplied rooms."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        selected = self.selected_clean_rooms.get(map_key, set())
        selected_room_ids = [room_id for room_id in room_ids if room_id in selected]
        return selected_room_ids or list(room_ids)

    def is_room_selected_for_clean(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> bool:
        """Return True when a room is selected for the next vacuum start."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        return room_id in self.selected_clean_rooms.get(map_key, set())

    def set_room_selected_for_clean(
        self,
        room_id: int,
        selected: bool,
        *,
        map_id: str | None = None,
    ) -> None:
        """Set whether a room is included in the next vacuum start."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        selected_rooms = self.selected_clean_rooms.setdefault(map_key, set())
        if selected:
            selected_rooms.add(room_id)
            return
        selected_rooms.discard(room_id)
        if not selected_rooms:
            self.selected_clean_rooms.pop(map_key, None)

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
        self._prev_working_status = state.working_status

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
        await super().async_shutdown()
