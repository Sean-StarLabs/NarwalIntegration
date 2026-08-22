"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .narwal_client import (
    CleaningRoute,
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

from .const import DOMAIN, NO_BROADCAST_PRODUCT_KEYS

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
ACTIVE_TASK_REFRESH_INTERVAL = 30.0
REMAP_MAP_REFRESH_ATTEMPTS = 3
REMAP_MAP_REFRESH_RETRY_DELAY = 10.0
PENDING_ROOM_PLAN_TTL = 120.0
ROOM_CLEAN_SETTING_ATTRS = frozenset(field.name for field in fields(RoomCleanSettings))
MOP_WORK_MODES = frozenset(
    {WorkMode.MOP, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)
VACUUM_WORK_MODES = frozenset(
    {WorkMode.VACUUM, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)


@dataclass
class CleanSettings(RoomCleanSettings):
    """User-selected clean parameters, applied at the next room clean start.

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
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
    ) and not _state_attr_is_true(state, "is_returning")


def is_clean_session_context(state: NarwalState | None) -> bool:
    """Return True while robot-side clean task context is still current."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
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


def clean_setting_applies_to_mode(attr: str, work_mode: WorkMode) -> bool:
    """Return True when a clean setting is meaningful for the selected mode."""
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
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
        or _state_attr_is_true(state, "is_returning")
        or _state_attr_is_true(state, "is_charging_to_resume")
        or _state_attr_is_true(state, "is_station_active")
    )


def can_edit_pending_clean_settings(state: NarwalState | None) -> bool:
    """Return True when pending next-clean settings can be edited locally."""
    if has_blocking_error(state):
        return False
    return not is_narwal_task_busy(state)


def can_start_cleaning(state: NarwalState | None) -> bool:
    """Return True when a new robot clean command can be sent now."""
    if has_blocking_error(state):
        return False
    return state.is_docked and not is_narwal_task_busy(state)


def is_setup_available(state: NarwalState | None) -> bool:
    """Return True when start-time clean setup controls should be available."""
    if state is None or state.working_status in (
        WorkingStatus.UNKNOWN,
        WorkingStatus.ERROR,
    ):
        return False
    return not is_narwal_task_busy(state)


def _map_refresh_key(map_data: object | None) -> tuple | None:
    """Return a compact key for detecting static map replacement."""
    if map_data is None:
        return None
    compressed = getattr(map_data, "compressed_map", b"")
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
        getattr(map_data, "map_id", None),
        getattr(map_data, "created_at", None) or 0,
        getattr(map_data, "width", None),
        getattr(map_data, "height", None),
        getattr(map_data, "resolution", None),
        digest,
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
        self.active_room_ids: list[int] | None = None
        self._active_room_plan_pending_until = 0.0
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        self._remapping_map_key: tuple | None = None
        self._remapping_map_refresh_pending = False
        self._remapping_map_refresh_attempts = 0
        self._remapping_map_next_refresh = 0.0
        self._last_display_map_resub: float = 0.0
        self._last_topic_subscribe: float = 0.0
        self._last_task_details_refresh: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable
        self._consumable_poll_countdown = 0  # 0 = fetch on next poll, then every CONSUMABLE_POLL_EVERY

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

    def set_active_room_ids(self, room_ids: list[int] | None) -> None:
        """Store the room plan requested for the current cleaning task."""
        if room_ids is None:
            self.active_room_ids = None
            self._active_room_plan_pending_until = 0.0
            return
        clean_room_ids = [room_id for room_id in room_ids if room_id > 0]
        self.active_room_ids = clean_room_ids or None
        self._active_room_plan_pending_until = (
            time.monotonic() + PENDING_ROOM_PLAN_TTL
            if self.active_room_ids else 0.0
        )

    def _sync_active_room_ids(self, state: NarwalState) -> None:
        """Clear active-room context when the active clean has ended."""
        if state.working_status == WorkingStatus.ERROR:
            self.set_active_room_ids(None)
            return
        if is_clean_session_context(state):
            self._active_room_plan_pending_until = 0.0
            return
        active_room_ids = getattr(self, "active_room_ids", None)
        pending_until = getattr(self, "_active_room_plan_pending_until", 0.0)
        if (
            active_room_ids
            and pending_until > time.monotonic()
        ):
            return
        self.set_active_room_ids(None)

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
            await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
        except Exception:
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
                if await self.client.subscribe_to_topics():
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
        self._sync_active_room_ids(state)
        previous_status = self._prev_working_status
        is_remapping = state.working_status == WorkingStatus.REMAPPING

        if is_remapping and previous_status != WorkingStatus.REMAPPING:
            self._remapping_map_key = _map_refresh_key(state.map_data)
            self._remapping_map_refresh_pending = True
            self._remapping_map_refresh_attempts = 0
            self._remapping_map_next_refresh = 0.0
        elif (
            is_remapping
            and self._remapping_map_refresh_pending
            and self._remapping_map_key is None
        ):
            self._remapping_map_key = _map_refresh_key(state.map_data)

        if (
            not is_remapping
            and previous_status == WorkingStatus.REMAPPING
            and self._remapping_map_refresh_pending
        ):
            self._remapping_map_refresh_attempts = 0
            self._remapping_map_next_refresh = 0.0

        if (
            not is_remapping
            and self._remapping_map_refresh_pending
            and self._remapping_map_refresh_attempts < REMAP_MAP_REFRESH_ATTEMPTS
            and not self._map_fetch_pending
            and time.monotonic() >= self._remapping_map_next_refresh
        ):
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_static_map(
                    reason="remapping",
                    previous_key=self._remapping_map_key,
                ),
                f"{DOMAIN}_map_fetch",
            )

        # Fetch static map if missing (get_map failed at startup)
        if state.map_data is None and not self._map_fetch_pending:
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_static_map(reason="missing"),
                f"{DOMAIN}_map_fetch",
            )

        # Detect return-to-dock transition: CLEANING/CLEANING_ALT → docked state.
        # Broadcast dock fields are stale after docking — immediate poll
        # refreshes them so UI shows DOCKED instead of IDLE.
        # On older FW the transition is → STANDBY; on v01.07.23+ it may
        # go directly to DOCKED_V2(2).
        if (
            state.working_status in (
                WorkingStatus.STANDBY,
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
                WorkingStatus.DOCKED_V2,
            )
            and previous_status
            in (*ACTIVE_CLEANING_STATUSES, WorkingStatus.TASK_COMPLETED)
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
        if is_cleaning or is_remapping:
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

        if is_cleaning or state.is_station_active:
            now = time.monotonic()
            if now - self._last_task_details_refresh > ACTIVE_TASK_REFRESH_INTERVAL:
                self._last_task_details_refresh = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._refresh_task_details(cleaning=is_cleaning),
                    f"{DOMAIN}_task_details",
                )

        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Broadcast received (status=%s) — normal polling restored",
                state.working_status.name,
            )

    async def _fetch_static_map(
        self,
        *,
        reason: str,
        previous_key: tuple | None = None,
    ) -> None:
        """Fetch static map after startup misses or remapping changes."""
        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Map fetch failed — will retry on next broadcast")
            self._map_fetch_pending = False
            if reason == "remapping":
                self._remapping_map_next_refresh = (
                    time.monotonic() + REMAP_MAP_REFRESH_RETRY_DELAY
                )
            return
        self._map_fetch_pending = False

        if reason == "remapping":
            self._remapping_map_refresh_attempts += 1
            new_key = _map_refresh_key(self.client.state.map_data)
            if new_key is not None and (previous_key is None or new_key != previous_key):
                self._remapping_map_refresh_pending = False
                self._remapping_map_key = None
                self._remapping_map_refresh_attempts = 0
                self._remapping_map_next_refresh = 0.0
                _LOGGER.info("Static map refreshed after remapping")
            elif self._remapping_map_refresh_attempts >= REMAP_MAP_REFRESH_ATTEMPTS:
                self._remapping_map_refresh_pending = False
                self._remapping_map_key = None
                self._remapping_map_next_refresh = 0.0
                _LOGGER.debug(
                    "Static map unchanged after remapping refresh attempts"
                )
            else:
                self._remapping_map_next_refresh = (
                    time.monotonic() + REMAP_MAP_REFRESH_RETRY_DELAY
                )
                _LOGGER.debug("Static map unchanged after remapping; will retry")
        else:
            if (
                reason == "missing"
                and self.client.state.working_status == WorkingStatus.REMAPPING
                and self._remapping_map_refresh_pending
                and self._remapping_map_key is None
            ):
                self._remapping_map_key = _map_refresh_key(self.client.state.map_data)
            _LOGGER.info("Static map loaded (was missing at startup)")

        if self.client.supports_broadcasts:
            try:
                if await self.client.subscribe_to_topics():
                    self._last_topic_subscribe = time.monotonic()
            except Exception:
                _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _fetch_missing_map(self) -> None:
        """Fetch static map when it's missing (get_map failed at startup)."""
        await self._fetch_static_map(reason="missing")

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        if not self.client.supports_broadcasts:
            return
        try:
            if await self.client.subscribe_to_topics():
                self._last_topic_subscribe = time.monotonic()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _refresh_task_details(self, *, cleaning: bool) -> None:
        """Query app-style task detail endpoints while a task is active."""
        try:
            if cleaning:
                await self.client.get_clean_progress_info()
            else:
                await self.client.get_dry_mop_remain_time()
            await self.client.get_robot_task_status()
        except Exception as err:
            _LOGGER.debug("Task detail refresh failed: %s", err)
            return
        self.async_set_updated_data(self.client.state)

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            self.async_set_updated_data(self.client.state)
        except Exception:
            _LOGGER.debug("Failed to refresh dock status after transition")

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
            await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
        except Exception as err:
            self._consecutive_failures += 1
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

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            try:
                await self.client.get_map()
            except Exception:
                pass

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
                if await self.client.subscribe_to_topics():
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

        is_cleaning = (
            self.client.state.is_cleaning
            or self.client.state.has_recent_active_working_status
            or (
                not self.client.state.is_docked
                and self.client.state.working_status in ACTIVE_CLEANING_STATUSES
            )
        )
        if is_cleaning or self.client.state.is_station_active:
            now = time.monotonic()
            if now - self._last_task_details_refresh > ACTIVE_TASK_REFRESH_INTERVAL:
                self._last_task_details_refresh = now
                await self._refresh_task_details(cleaning=is_cleaning)

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        self._sync_active_room_ids(self.client.state)
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
