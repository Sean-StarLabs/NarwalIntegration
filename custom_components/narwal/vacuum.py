"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging

from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)

try:
    from homeassistant.components.vacuum import Segment
except ImportError:
    Segment = None  # HA < 2026.3 — room cleaning unavailable
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .narwal_client import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalCommandError,
    WorkMode,
    WorkingStatus,
)
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

from . import NarwalConfigEntry
from .const import FAN_SPEED_LIST, FAN_SPEED_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)

ROOM_CLEAN_MODES = {
    "Vacuum": WorkMode.VACUUM,
    "Mop": WorkMode.MOP,
    "Vacuum then mop": WorkMode.VACUUM_THEN_MOP,
    "Vacuum and mop": WorkMode.VACUUM_AND_MOP,
}
ROOM_CLEAN_SUCTION = {"AI": FanLevel.UNSPECIFIED, **FAN_SPEED_MAP}
ROOM_CLEAN_WATER = {
    "Dry": MopHumidity.DRY,
    "Normal": MopHumidity.NORMAL,
    "Wet": MopHumidity.WET,
}
ROOM_CLEAN_SCRUB = {
    "Normal": MopStrengthLevel.NORMAL,
    "High": MopStrengthLevel.HIGH,
}
ROOM_CLEAN_ROUTES = {
    "Standard": CleaningRoute.STANDARD,
    "Meticulous": CleaningRoute.METICULOUS,
}

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_V2: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_FLOW2: VacuumActivity.CLEANING,
    WorkingStatus.TASK_COMPLETED: VacuumActivity.RETURNING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "vacuum"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    ) | (VacuumEntityFeature.CLEAN_AREA if Segment is not None else VacuumEntityFeature(0))
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["device_id"]
        self._last_fan_speed: str | None = None

    @property
    def activity(self) -> VacuumActivity:
        """Return the current vacuum activity."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        if state.working_status == WorkingStatus.ERROR:
            return VacuumActivity.ERROR
        is_cleaning_state = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
            or state.task_active
        )
        # is_paused (field 3.2) stays stale after docking — only trust
        # during cleaning states. Paused takes priority over returning
        # since the robot physically stops when paused mid-return.
        if state.task_active and (state.is_paused or state.task_paused):
            return VacuumActivity.PAUSED
        # Robot keeps working_status=CLEANING while navigating back to dock
        # (field 3.7=1 indicates returning).
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.task_active:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED
        if state.is_paused and is_cleaning_state:
            return VacuumActivity.PAUSED
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity is not None:
            return activity
        if state.working_status == WorkingStatus.UNKNOWN and not state.is_docked:
            _LOGGER.warning(
                "Unknown working status while off-dock; reporting cleaning"
            )
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed.

        The robot protocol does not broadcast the active fan speed setting,
        so we track the last value set via the integration. Returns None
        until the user sets a fan speed for the first time.
        """
        return self._last_fan_speed

    # Timeout for action commands (start/stop/return) — robot may need
    # time to load map, plan route, etc., especially after waking.
    _ACTION_TIMEOUT = 10.0

    async def _ensure_awake(self) -> None:
        """Wake the robot if it is not broadcasting.

        Sends a wake burst and waits for broadcasts. If the robot doesn't
        respond, the command is still attempted — it may work even without
        a wake confirmation (e.g., shallow sleep).
        """
        client = self.coordinator.client
        if not client.robot_awake:
            _LOGGER.debug("Robot not awake — sending wake burst")
            await client.wake(timeout=10.0)

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        await self._ensure_awake()
        state = self.coordinator.data
        if state and state.task_active and (state.is_paused or state.task_paused):
            resp = await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
            _LOGGER.info(
                "Resume command response: code=%s, success=%s",
                resp.result_code, resp.success,
            )
            if resp.success:
                state.mark_task_resumed()
                self.coordinator.client._last_active_working_status_time = (
                    state.last_active_working_status_time
                )
                self.coordinator.async_set_updated_data(state)
        else:
            resp = await self.coordinator.client.start()
            _LOGGER.info(
                "Start command response: code=%s, success=%s",
                resp.result_code, resp.success,
            )
            if resp.success:
                self.coordinator.async_set_updated_data(
                    self.coordinator.client.state
                )
            else:
                _LOGGER.warning(
                    "Start command did not succeed (code=%s) — robot may not have started",
                    resp.result_code,
                )

    async def async_stop(self, **kwargs) -> None:
        """Stop cleaning."""
        await self._ensure_awake()
        resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop response: code=%s, success=%s", resp.result_code, resp.success)
        if resp.success:
            self.coordinator.client.state.clear_active_task()
            self.coordinator.async_set_updated_data(self.coordinator.client.state)

    async def async_pause(self) -> None:
        """Pause cleaning."""
        resp = await self.coordinator.client.pause()
        _LOGGER.info("Pause response: code=%s, success=%s", resp.result_code, resp.success)
        if resp.success:
            self.coordinator.client.state.mark_task_paused()
            self.coordinator.async_set_updated_data(self.coordinator.client.state)

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        await self._ensure_awake()
        resp = await self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Return-to-base response: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        if not resp.success:
            _LOGGER.warning(
                "Return-to-base did not succeed (code=%s)", resp.result_code,
            )
        elif self.coordinator.client.state.task_active:
            self.coordinator.client.state.mark_task_paused()
            self.coordinator.async_set_updated_data(self.coordinator.client.state)

    async def async_locate(self, **kwargs) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        await self._ensure_awake()
        await self.coordinator.client.locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Set the fan speed."""
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is not None:
            await self.coordinator.client.set_fan_speed(level)
            self._last_fan_speed = next(
                (
                    label
                    for label in FAN_SPEED_LIST
                    if FAN_SPEED_MAP[label] == level
                ),
                fan_speed,
            )
            self.async_write_ha_state()

    # --- Segment API (HA 2026.3 room-specific cleaning) ---

    async def async_get_segments(self) -> list:
        """Return cleanable room segments from map data.

        Maps RoomInfo from get_map to HA Segment objects.
        Room names match the Narwal app exactly (RoomInfo.display_name).
        Falls back to HA-cached last_seen_segments when map data is not yet
        loaded (robot asleep at startup), so clean_area works without waking
        the robot first.
        Returns [] when HA < 2026.3 (Segment class unavailable).
        """
        if Segment is None:
            return []
        state = self.coordinator.data
        if state is None or state.map_data is None:
            # Robot sleeping — return cached segments so clean_area still works
            last = getattr(self, "last_seen_segments", None)
            return list(last) if last else []
        return [
            Segment(
                id=str(room.room_id),
                name=room.display_name,
                group="Rooms" if room.category == 1 else "Utility" if room.category == 2 else None,
            )
            for room in state.map_data.rooms
            if room.room_id > 0
        ]

    async def async_clean_segments(
        self, segment_ids: list[str], **kwargs: Any
    ) -> None:
        """Clean specific rooms by segment IDs.

        Converts string segment IDs back to integer room IDs and sends
        a room-specific clean command to the robot.
        """
        await self._ensure_awake()
        room_ids = [int(sid) for sid in segment_ids]
        _LOGGER.info("Starting room-specific clean: rooms=%s", room_ids)
        settings = self.coordinator.select_options
        resp = await self.coordinator.client.start_rooms(
            room_ids,
            work_mode=ROOM_CLEAN_MODES[settings.get("mode", "Vacuum and mop")],
            fan=ROOM_CLEAN_SUCTION[settings.get("suction", "AI")],
            water=ROOM_CLEAN_WATER[settings.get("water", "Wet")],
            mop_strength=ROOM_CLEAN_SCRUB[settings.get("scrub", "High")],
            passes=int(settings.get("passes", "2")),
            route=ROOM_CLEAN_ROUTES[settings.get("route", "Meticulous")],
        )
        try:
            result_name = CommandResult(resp.result_code).name
        except ValueError:
            result_name = f"UNKNOWN({resp.result_code})"
        _LOGGER.info(
            "Room clean response: %s (code=%s), rooms=%s",
            result_name, resp.result_code, room_ids,
        )
        if not resp.success:
            _LOGGER.warning(
                "Room clean failed: %s (code=%s), rooms=%s. "
                "CONFLICT means robot is busy (cleaning, returning, or docked cycle in progress). "
                "NOT_APPLICABLE means robot cannot clean right now. "
                "Try again after the robot is idle on the dock.",
                result_name, resp.result_code, room_ids,
            )
        else:
            self.coordinator.async_set_updated_data(
                self.coordinator.client.state
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._check_segment_changes()
        super()._handle_coordinator_update()

    def _check_segment_changes(self) -> None:
        """Detect segment changes and raise repair issue if needed.

        Compares current room data against last_seen_segments (managed by HA).
        If rooms have changed (added, removed, or renamed), creates a repair
        issue so the user can update their segment-to-area mappings.
        """
        last = getattr(self, "last_seen_segments", None)
        if last is None:
            return  # No mapping configured yet
        state = self.coordinator.data
        if state is None or state.map_data is None:
            return
        current_set = {
            (str(r.room_id), r.display_name)
            for r in state.map_data.rooms
            if r.room_id > 0
        }
        last_set = {(s.id, s.name) for s in last}
        if current_set != last_set:
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set), len(current_set),
            )
            self.async_create_segments_issue()
