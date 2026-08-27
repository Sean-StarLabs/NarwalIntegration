"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

try:
    from homeassistant.components.vacuum import Segment
except ImportError:
    Segment = None  # HA < 2026.3 — room cleaning unavailable
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import FAN_SPEED_LIST, FAN_SPEED_MAP, fan_speed_list_for
from .coordinator import NarwalCoordinator
from .dock_tasks import (
    can_start_robot_clean,
    can_stop_dock_task,
    has_active_dock_work,
    is_clean_session_context,
)
from .entity import NarwalEntity
from .narwal_client import CommandResult, WorkingStatus
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

_LOGGER = logging.getLogger(__name__)

# working_status values already reported as unmapped. Activity is recomputed on
# every state broadcast, so warn once per distinct value instead of flooding.
_WARNED_UNMAPPED_ACTIVITY: set[int] = set()

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING_V2: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    # Mapping/exploration is active robot work.
    WorkingStatus.REMAPPING: VacuumActivity.CLEANING,
    WorkingStatus.TASK_COMPLETED: VacuumActivity.RETURNING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


def _result_name(result_code: int | CommandResult) -> str:
    """Return a readable Narwal command result name."""
    if result_code == 0:
        return "ACCEPTED"
    try:
        return CommandResult(result_code).name
    except ValueError:
        return f"UNKNOWN({result_code})"


# FanLevel value -> fan_speed label. FAN_SPEED_MAP also holds back-compat aliases.
_FAN_LABELS: dict[int, str] = {int(FAN_SPEED_MAP[label]): label for label in FAN_SPEED_LIST}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, RestoreEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "vacuum"
    _BASE_SUPPORTED_FEATURES = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    ) | (VacuumEntityFeature.CLEAN_AREA if Segment is not None else VacuumEntityFeature(0))

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Hide robot actions while dock work makes them unsafe or ambiguous."""
        features = self._BASE_SUPPORTED_FEATURES
        state = self.coordinator.data
        if not has_active_dock_work(state):
            return features
        features &= ~(
            VacuumEntityFeature.START
            | VacuumEntityFeature.PAUSE
            | VacuumEntityFeature.RETURN_HOME
            | VacuumEntityFeature.LOCATE
            | VacuumEntityFeature.CLEAN_AREA
        )
        if not is_clean_session_context(state):
            features &= ~VacuumEntityFeature.STOP
        return features

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["device_id"]
        # Offered tiers are per-model: models whose app tops out at DEEP don't get "Ultra".
        self._attr_fan_speed_list = fan_speed_list_for(coordinator.config_entry.data)

    async def async_added_to_hass(self) -> None:
        """Restore the pending fan speed into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and (fan := last.attributes.get("fan_speed")) in FAN_SPEED_MAP:
            self.coordinator.clean_settings.fan = FAN_SPEED_MAP[fan]

    @property
    def activity(self) -> VacuumActivity:
        """Return the current vacuum activity."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        is_cleaning_state = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
        )
        # is_paused (field 3.2) stays stale after docking — only trust
        # during cleaning states. Paused takes priority over returning
        # since the robot physically stops when paused mid-return.
        if state.is_paused and is_cleaning_state:
            return VacuumActivity.PAUSED
        # Check returning before cleaning — robot keeps working_status=CLEANING
        # while navigating back to dock (field 3.7=1 indicates returning)
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity is not None:
            return activity
        # Unknown working_status value: infer from dock signals so we don't
        # report IDLE while the robot is clearly active off-dock. New firmware
        # versions may introduce values we haven't mapped yet.
        if not state.is_docked:
            if state.working_status.value not in _WARNED_UNMAPPED_ACTIVITY:
                _WARNED_UNMAPPED_ACTIVITY.add(state.working_status.value)
                _LOGGER.warning(
                    "Unmapped working_status %s (%d) while off-dock; reporting "
                    "CLEANING (further occurrences are suppressed)",
                    state.working_status.name,
                    state.working_status.value,
                )
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the selected fan speed.

        The robot does not broadcast the active fan level, so this reflects the
        pending value held in coordinator.clean_settings (applied at the next clean
        and, while cleaning, written live via set_fan_speed).
        """
        return _FAN_LABELS.get(int(self.coordinator.clean_settings.fan))

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

    async def _validate_clean_start(self) -> None:
        """Refresh dock state and reject starts that conflict with dock work."""
        if not await self.coordinator.async_refresh_dock_status():
            raise HomeAssistantError("Narwal status could not be refreshed")
        if not can_start_robot_clean(self.coordinator.client.state):
            raise HomeAssistantError("Narwal dock task is active")

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        await self._ensure_awake()
        state = self.coordinator.data
        # is_paused stays stale after docking — only trust it during cleaning
        is_cleaning = state and state.working_status in ACTIVE_CLEANING_STATUSES
        if is_cleaning and state.is_paused:
            await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
            return
        room_ids: list[int] = []
        async with self.coordinator.dock_action_lock:
            await self._validate_clean_start()

            # Whole-house clean enumerates every room via clean/start_clean, matching the
            # app's allRoomIds() path. clean/plan/start (StartWithPlan) would instead re-run
            # the saved current plan — i.e. the last room selection — not the whole house.
            room_ids = await self._all_room_ids()
            if room_ids:
                settings = self.coordinator.clean_settings
                resp = await self.coordinator.client.start_rooms(
                    room_ids,
                    work_mode=settings.work_mode,
                    fan=settings.fan,
                    water=settings.water,
                    mop_strength=settings.mop_strength,
                    passes=settings.passes,
                )
            else:
                # No map rooms known — best-effort fall back to the saved-plan start.
                resp = await self.coordinator.client.start()
            if resp.accepted:
                self.coordinator.client.state.assume_robot_clean()
                self.coordinator.async_set_updated_data(self.coordinator.client.state)
        _LOGGER.info(
            "Whole-house start: code=%s, success=%s, rooms=%s",
            resp.result_code, resp.success, room_ids or "(saved plan)",
        )
        if not resp.accepted:
            _LOGGER.warning(
                "Start command was rejected: %s (code=%s)",
                _result_name(resp.result_code),
                resp.result_code,
            )
            raise HomeAssistantError(
                f"Narwal start command failed: {_result_name(resp.result_code)}"
            )

    async def _all_room_ids(self) -> list[int]:
        """Every cleanable room id for a whole-house clean; fetches the map if not cached."""
        state = self.coordinator.data
        if state is None or state.map_data is None:
            try:
                await self.coordinator.client.get_map()
            except Exception:  # noqa: BLE001 — best-effort prefetch; fall through to fallback
                _LOGGER.debug("get_map for whole-house clean failed")
            state = self.coordinator.data
        if state and state.map_data:
            return [r.room_id for r in state.map_data.rooms if r.room_id > 0]
        # Map still unavailable — reuse the HA segment cache (Segment.id == str(room_id)).
        cached = getattr(self, "last_seen_segments", None) or []
        ids: list[int] = []
        for seg in cached:
            try:
                ids.append(int(seg.id))
            except (ValueError, AttributeError, TypeError):
                continue
        return ids

    async def async_stop(self, **kwargs) -> None:
        """Stop cleaning."""
        await self._ensure_awake()
        async with self.coordinator.dock_action_lock:
            refreshed = await self.coordinator.async_refresh_dock_status()
            state = self.coordinator.client.state
            clean_context = is_clean_session_context(state)
            if state.has_unmapped_active_dock_task and not clean_context:
                raise HomeAssistantError(
                    "Narwal dock task cannot be stopped safely right now"
                )
            if state.is_station_active and not clean_context:
                if not can_stop_dock_task(state):
                    raise HomeAssistantError(
                        "Narwal dock task cannot be stopped safely right now"
                    )
                resp = await self.coordinator.client.stop_dock_task()
            elif not clean_context:
                if not refreshed:
                    raise HomeAssistantError("Narwal status could not be refreshed")
                raise HomeAssistantError("Narwal has no active task to stop")
            else:
                resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop response: code=%s, success=%s", resp.result_code, resp.success)
        if not resp.accepted:
            raise HomeAssistantError(
                f"Narwal stop command failed: {_result_name(resp.result_code)}"
            )

    async def async_pause(self) -> None:
        """Pause cleaning."""
        async with self.coordinator.dock_action_lock:
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if has_active_dock_work(self.coordinator.client.state):
                raise HomeAssistantError("Narwal dock task is active")
            resp = await self.coordinator.client.pause()
        _LOGGER.info("Pause response: code=%s, success=%s", resp.result_code, resp.success)

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        await self._ensure_awake()
        async with self.coordinator.dock_action_lock:
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if has_active_dock_work(self.coordinator.client.state):
                raise HomeAssistantError("Narwal dock task is active")
            resp = await self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Return-to-base response: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        if not resp.accepted:
            _LOGGER.warning(
                "Return-to-base did not succeed: %s (code=%s)",
                _result_name(resp.result_code),
                resp.result_code,
            )

    async def async_locate(self, **kwargs) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        await self._ensure_awake()
        async with self.coordinator.dock_action_lock:
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if has_active_dock_work(self.coordinator.client.state):
                raise HomeAssistantError("Narwal dock task is active")
            await self.coordinator.client.locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Set the fan speed.

        Stores it as the pending suction for the next clean; if the robot is
        currently cleaning, also writes it live via set_fan_speed.
        """
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            return
        self.coordinator.clean_settings.fan = level
        self.async_write_ha_state()
        state = self.coordinator.data
        if state is not None and state.is_cleaning:
            await self.coordinator.client.set_fan_speed(level)

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
        try:
            room_ids = [int(sid) for sid in segment_ids]
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal segment IDs must be numeric") from err

        async with self.coordinator.dock_action_lock:
            await self._validate_clean_start()
            try:
                await self.coordinator.client.get_map()
            except Exception:
                _LOGGER.debug("Could not refresh Narwal map before segment clean")
            state = self.coordinator.client.state
            known_room_ids = {
                room.room_id
                for room in getattr(getattr(state, "map_data", None), "rooms", ())
                if room.room_id > 0
            }
            if known_room_ids:
                unknown_room_ids = [
                    room_id for room_id in room_ids if room_id not in known_room_ids
                ]
                if unknown_room_ids:
                    raise HomeAssistantError(
                        f"Unknown Narwal room ID: {', '.join(map(str, unknown_room_ids))}"
                    )
            settings = self.coordinator.clean_settings
            _LOGGER.info(
                "Starting room-specific clean: rooms=%s mode=%s fan=%s water=%s "
                "mop_strength=%s passes=%s",
                room_ids, settings.work_mode.name, settings.fan.name,
                settings.water.name, settings.mop_strength.name, settings.passes,
            )
            resp = await self.coordinator.client.start_rooms(
                room_ids,
                work_mode=settings.work_mode,
                fan=settings.fan,
                water=settings.water,
                mop_strength=settings.mop_strength,
                passes=settings.passes,
            )
            if resp.accepted:
                self.coordinator.client.state.assume_robot_clean()
                self.coordinator.async_set_updated_data(self.coordinator.client.state)
        result_name = _result_name(resp.result_code)
        _LOGGER.info(
            "Room clean response: %s (code=%s), rooms=%s",
            result_name, resp.result_code, room_ids,
        )
        if not resp.accepted:
            _LOGGER.warning(
                "Room clean failed: %s (code=%s), rooms=%s. "
                "CONFLICT means robot is busy (cleaning, returning, or docked cycle in progress). "
                "NOT_APPLICABLE means robot cannot clean right now. "
                "Try again after the robot is idle on the dock.",
                result_name, resp.result_code, room_ids,
            )
            raise HomeAssistantError(
                f"Narwal room clean failed: {result_name}"
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
