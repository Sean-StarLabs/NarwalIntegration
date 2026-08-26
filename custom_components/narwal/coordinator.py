"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, NO_BROADCAST_PRODUCT_KEYS
from .narwal_client import (
    CommandResponse,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalClient,
    NarwalConnectionError,
    NarwalState,
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
    return isinstance(field3, dict)


@dataclass
class CleanSettings:
    """User-selected clean parameters, applied at the next room clean start.

    Single source of truth the select/number entities mutate and the
    clean-start path reads; each entity persists its value via RestoreEntity.
    Only fan and water also have live setters.
    """

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1


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
            self.async_set_updated_data(self.client.state)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status after transition")
            self.async_set_updated_data(self.client.state)

    async def async_refresh_dock_status(self) -> bool:
        """Refresh full dock/base-station status for action gating."""
        try:
            response = await self.client.get_status(full_update=True)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status")
            self.async_set_updated_data(self.client.state)
            return False
        if not response.accepted:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug(
                "Dock status refresh was rejected with code %s",
                response.result_code,
            )
            self.async_set_updated_data(self.client.state)
            return False
        if not _has_dock_status_payload(response):
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Dock status refresh returned no dock-status payload")
            self.async_set_updated_data(self.client.state)
            return False
        self._mark_dock_status_refresh_succeeded()
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

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
