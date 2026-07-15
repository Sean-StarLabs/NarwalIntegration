"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import NarwalCloudClient, NarwalCloudConsumable, NarwalCloudError
from .const import (
    CLOUD_CONSUMABLES_POLL_HOURS,
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_REGION,
    CONF_PRODUCT_KEY,
    DEFAULT_CLOUD_REGION,
    DOMAIN,
    is_maintenance_alerts_supported,
)
from .narwal_client import NarwalClient, NarwalConnectionError, NarwalState
from .narwal_client.const import ACTIVE_CLEANING_STATUSES, WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal
ACTIVE_TASK_REFRESH_INTERVAL = 10.0
MAINTENANCE_REFRESH_INTERVAL_SECONDS = 900.0
CLOUD_CONSUMABLES_POLL_INTERVAL = timedelta(hours=CLOUD_CONSUMABLES_POLL_HOURS)


def _response_has_active_task(response: object, *, task_active: bool = False) -> bool:
    """Return whether a task-status response reports an active clean."""
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        return False
    payload = data.get("2")
    if not isinstance(payload, dict):
        return False
    try:
        progress = int(payload.get("1"))
    except (TypeError, ValueError):
        return False
    return 0 < progress < 100 or (progress == 0 and task_active)


def _refresh_active_task_marker(client: NarwalClient) -> None:
    """Keep state and stale-base suppression on the same active timestamp."""
    client.state.refresh_active_cleaning()
    client._last_active_working_status_time = (
        client.state.last_active_working_status_time
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
        self.client = NarwalClient(
            host=entry.data["host"],
            port=entry.data["port"],
            device_id=entry.data.get("device_id", ""),
            topic_prefix=topic_prefix,
        )
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        self._last_display_map_resub: float = 0.0
        self._last_status_resub: float = 0.0
        self._last_task_details_refresh: float = 0.0
        self._last_maintenance_refresh: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable
        self.select_options: dict[str, str] = {}
        self.cloud_consumables: dict[str, NarwalCloudConsumable] = {}
        self.cloud_consumables_error: str | None = None
        self._cloud_consumables_last_update = 0.0
        self._cloud_consumables_lock = asyncio.Lock()
        self._cloud_client: NarwalCloudClient | None = None
        raw_options = getattr(entry, "options", {}) or {}
        entry_options = raw_options if isinstance(raw_options, Mapping) else {}
        cloud_email = (
            entry_options[CONF_CLOUD_EMAIL]
            if CONF_CLOUD_EMAIL in entry_options
            else entry.data.get(CONF_CLOUD_EMAIL)
        )
        cloud_password = (
            entry_options[CONF_CLOUD_PASSWORD]
            if CONF_CLOUD_PASSWORD in entry_options
            else entry.data.get(CONF_CLOUD_PASSWORD)
        )
        cloud_region = (
            entry_options[CONF_CLOUD_REGION]
            if CONF_CLOUD_REGION in entry_options
            else entry.data.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION)
        )
        self.cloud_credentials = (
            cloud_email or None,
            cloud_password or None,
            cloud_region,
        )
        if cloud_email and cloud_password:
            self._cloud_client = NarwalCloudClient(
                hass,
                email=cloud_email,
                password=cloud_password,
                region=cloud_region,
            )

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Queries initial state BEFORE starting the listener to avoid
        concurrent recv issues (see 446be16). Each command is wrapped in
        try/except so setup never crashes if the robot is asleep.
        The listener's keepalive loop handles waking independently.
        """
        _LOGGER.debug("Narwal setup start for %s", self.config_entry.title)
        await self.client.connect()
        _LOGGER.debug("Narwal connected for %s", self.config_entry.title)

        # Fetch initial state BEFORE starting listener (no concurrent recv)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info at startup")

        try:
            await self.client.get_status(full_update=True)
            if self.client.state.is_docked:
                task_status = await self.client.get_robot_task_status()
                if not _response_has_active_task(
                    task_status, task_active=self.client.state.task_active
                ):
                    await asyncio.sleep(0.25)
                    await self.client.get_robot_task_status()
        except Exception:
            _LOGGER.debug("Could not fetch initial status")

        await self._refresh_maintenance_details(force=True)

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        # Subscribe to broadcast topics (display_map, working_status, etc.)
        # Must be sent before listener starts so display_map flows during cleaning.
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Could not send topic subscription at startup")

        self.async_set_updated_data(self.client.state)

        # Set up push callback and start persistent listener
        self.client.on_state_update = self._on_state_update
        self._ensure_listener_running()

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name,
            state.battery_level,
            state.is_docked,
            self.client.robot_awake,
        )

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

        if self._cloud_client is not None:
            self.config_entry.async_create_background_task(
                self.hass,
                self._cloud_consumables_loop(),
                f"{DOMAIN}_cloud_consumables",
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
            and self._prev_working_status in ACTIVE_CLEANING_STATUSES
        ):
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._prev_working_status = state.working_status

        # display_map dropout recovery: if cleaning but no display_map for
        # 30s, re-send topic subscription. Only subscription — no wake burst
        # (wake bursts during cleaning cause pause bouncing).
        is_cleaning = (
            not state.is_station_active
            and (
                state.is_cleaning
                or (
                    not state.is_docked
                    and state.working_status in ACTIVE_CLEANING_STATUSES
                )
                or state.has_recent_active_working_status
            )
        )
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            status_recovery_scheduled = False
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
            if now - self._last_status_resub > 30.0:
                _LOGGER.info("Refreshing active clean base status")
                self._last_status_resub = now
                self._last_task_details_refresh = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._recover_status_broadcasts(),
                    f"{DOMAIN}_status_recover",
                )
                status_recovery_scheduled = True
            if (
                not status_recovery_scheduled
                and now - getattr(self, "_last_task_details_refresh", 0.0)
                > ACTIVE_TASK_REFRESH_INTERVAL
            ):
                self._last_task_details_refresh = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._refresh_task_details(cleaning=True),
                    f"{DOMAIN}_task_details",
                )
        elif state.is_station_active:
            now = time.monotonic()
            if now - getattr(self, "_last_task_details_refresh", 0.0) > 30.0:
                self._last_task_details_refresh = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._refresh_task_details(cleaning=False),
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

    def _ensure_listener_running(self) -> None:
        """Start the WebSocket listener if it is not already active."""
        if self._listen_task is not None and not self._listen_task.done():
            return
        self.client.on_state_update = self._on_state_update
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
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
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _recover_status_broadcasts(self) -> None:
        """Recover missing status/base broadcasts while display_map still flows."""
        try:
            await self.client.subscribe_to_topics()
            await self.client.get_clean_progress_info()
            task_status = await self.client.get_robot_task_status()
        except Exception:
            _LOGGER.debug("Status broadcast recovery failed")
            return
        if _response_has_active_task(
            task_status, task_active=self.client.state.task_active
        ):
            _refresh_active_task_marker(self.client)
        self.async_set_updated_data(self.client.state)

    async def _refresh_task_details(self, *, cleaning: bool) -> None:
        """Query app-style task detail endpoints while a task is active."""
        try:
            if cleaning:
                await self.client.get_clean_progress_info()
            else:
                await self.client.get_dry_mop_remain_time()
            task_status = await self.client.get_robot_task_status()
        except Exception as err:
            _LOGGER.debug("Task detail refresh failed: %s", err)
            return
        if cleaning and _response_has_active_task(
            task_status, task_active=self.client.state.task_active
        ):
            _refresh_active_task_marker(self.client)
        self.async_set_updated_data(self.client.state)

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            await self.client.get_status(full_update=True)
            if self.client.state.is_docked:
                await self.client.get_robot_task_status()
            self.async_set_updated_data(self.client.state)
        except Exception:
            _LOGGER.debug("Failed to refresh dock status after transition")

    async def _refresh_maintenance_details(self, *, force: bool = False) -> None:
        """Refresh maintenance counters exposed by the task-detail endpoint."""
        device_info = self.client.state.device_info
        if not is_maintenance_alerts_supported(
            self.config_entry.data,
            device_info.product_key if device_info else None,
        ):
            return
        now = time.monotonic()
        if (
            not force
            and self._last_maintenance_refresh > 0
            and now - self._last_maintenance_refresh
            < MAINTENANCE_REFRESH_INTERVAL_SECONDS
        ):
            return
        try:
            await self.client.get_clean_progress_info()
        except Exception as err:
            _LOGGER.debug("Maintenance detail refresh failed: %s", err)
            return
        self._last_maintenance_refresh = now

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived.

        Reconnection is handled by the listener loop's exponential backoff. If
        that listener has exited, restart it. Keep the last known data briefly,
        then mark the coordinator unavailable after the failure threshold.
        """
        try:
            if not self.client.connected:
                self._ensure_listener_running()
                raise NarwalConnectionError("Not connected")
            await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            if self.client.state.is_docked:
                await self.client.get_robot_task_status()
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures,
                self._max_failures,
                err,
            )
            return self.client.state
        else:
            self._consecutive_failures = 0

        await self._refresh_maintenance_details()

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            with suppress(Exception):
                await self.client.get_map()

        if self._cloud_consumables_due:
            await self.async_refresh_cloud_consumables()

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

    @property
    def _cloud_consumables_due(self) -> bool:
        """Return true when cloud consumables should be refreshed."""
        if getattr(self, "_cloud_client", None) is None:
            return False
        return (
            time.monotonic() - self._cloud_consumables_last_update
            >= CLOUD_CONSUMABLES_POLL_INTERVAL.total_seconds()
        )

    async def async_refresh_cloud_consumables(self) -> None:
        """Refresh read-only cloud consumables if cloud credentials are configured."""
        if self._cloud_client is None or self._cloud_consumables_lock.locked():
            return
        async with self._cloud_consumables_lock:
            self._cloud_consumables_last_update = time.monotonic()
            try:
                consumables = await self._cloud_client.async_get_consumables(
                    device_id=self.config_entry.data["device_id"],
                    product_id=self.config_entry.data[CONF_PRODUCT_KEY],
                )
            except NarwalCloudError as err:
                self.cloud_consumables_error = str(err)
                _LOGGER.warning(
                    "Narwal cloud consumables refresh failed for %s: %s",
                    self.config_entry.title,
                    err,
                )
                return
            except Exception as err:
                self.cloud_consumables_error = type(err).__name__
                _LOGGER.debug(
                    "Narwal cloud consumables refresh failed for %s",
                    self.config_entry.title,
                    exc_info=True,
                )
                return
            self.cloud_consumables = {
                item.code: item for item in consumables if item.has_life_counter
            }
            self.cloud_consumables_error = None
            _LOGGER.debug(
                "Loaded %d cloud consumables for %s",
                len(self.cloud_consumables),
                self.config_entry.title,
            )
            self.async_update_listeners()

    async def _cloud_consumables_loop(self) -> None:
        """Refresh cloud consumables independently from local status polling."""
        while True:
            await self.async_refresh_cloud_consumables()
            await asyncio.sleep(CLOUD_CONSUMABLES_POLL_INTERVAL.total_seconds())

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
