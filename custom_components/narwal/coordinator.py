"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .narwal_client import (
    WorkMode,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalClient,
    NarwalConnectionError,
    NarwalState,
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


@dataclass
class CleanSettings:
    """User-selected clean parameters, applied at the next room clean start.

    Single source of truth the select/number entities mutate and the clean-start path reads; each entity persists its value via RestoreEntity, so they survive restarts. Only fan and water also have live setters — work_mode/mop_strength/passes take effect at the next start.
    """

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1


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

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
