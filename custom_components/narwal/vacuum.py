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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .narwal_client import CommandResult, WorkingStatus
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

from . import NarwalConfigEntry
from .const import FAN_SPEED_LIST, FAN_SPEED_MAP, fan_speed_list_for
from .coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_locate_robot,
    can_pause_cleaning,
    can_resume_cleaning,
    can_return_home,
    can_start_cleaning,
    can_stop_cleaning,
    clean_setting_applies_to_mode,
    dock_task,
    is_live_clean_setting_available,
    is_narwal_task_busy,
    is_setup_available,
)
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)

# working_status values already reported as unmapped. Activity is recomputed on
# every state broadcast (~1.5s), so an unmapped value otherwise floods the log
# with thousands of identical lines (#46). Warn once per distinct value.
_WARNED_UNMAPPED_ACTIVITY: set[int] = set()

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING_V2: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.REMAPPING: VacuumActivity.CLEANING,  # mapping/exploration — robot is actively busy
    WorkingStatus.CUSTOM_CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.TASK_COMPLETED: VacuumActivity.RETURNING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}

# FanLevel value -> fan_speed label (canonical labels only; FAN_SPEED_MAP also holds back-compat aliases).
_FAN_LABELS: dict[int, str] = {int(FAN_SPEED_MAP[label]): label for label in FAN_SPEED_LIST}


def _result_name(result_code: int | CommandResult) -> str:
    """Return a readable Narwal command result name."""
    try:
        return CommandResult(result_code).name
    except ValueError:
        return f"UNKNOWN({result_code})"


def _raise_if_command_failed(response: Any, action: str) -> None:
    """Raise a Home Assistant service error for rejected robot commands."""
    if response.success:
        return
    raise HomeAssistantError(
        f"Narwal {action} failed: {_result_name(response.result_code)}"
    )


def _task_status(state: Any) -> str:
    """Return the same task-status value exposed by the status sensor."""
    is_cleaning_state = (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_recent_active_working_status
    )
    if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
        return "error"
    if state.working_status == WorkingStatus.REMAPPING:
        return "remapping"
    if state.is_paused and is_cleaning_state:
        return "paused"
    if state.is_returning:
        return "returning"
    if state.is_charging_to_resume:
        return "charging_to_resume"
    if state.is_station_active:
        return "station_active"
    if state.is_cleaning:
        return "cleaning"
    if state.is_docked:
        return "docked"
    if state.working_status == WorkingStatus.STANDBY:
        return "idle"
    return "unknown"


def _is_dock_side(state: Any) -> bool:
    """Return true when the robot or dock is doing dock-side work."""
    return state.is_docked or state.is_charging_to_resume or state.is_station_active


def _has_active_cleaning_metrics(state: Any) -> bool:
    """Return true while live clean-progress details are current."""
    return state.is_cleaning or state.has_recent_active_working_status


def _has_active_room_plan(state: Any) -> bool:
    """Return true while room-plan attributes still describe the active task."""
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_recent_active_working_status
        or state.is_returning
        or state.is_charging_to_resume
    )


def _charging_state(state: Any) -> str:
    """Return the same charging value exposed by the charging sensor."""
    if state.is_charging_to_resume:
        return "charging"
    if not state.is_docked:
        return "not_charging"
    if state.battery_level >= 100:
        return "fully_charged"
    return "charging"


def _room_names_by_id(state: Any) -> dict[int, str]:
    """Return cleanable room display names keyed by room id."""
    map_data = getattr(state, "map_data", None)
    if map_data is None:
        return {}
    return {
        room.room_id: room.display_name
        for room in map_data.rooms
        if room.room_id > 0
    }


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
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    ) | (VacuumEntityFeature.CLEAN_AREA if Segment is not None else VacuumEntityFeature(0))

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
        if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
            return VacuumActivity.ERROR
        is_cleaning_state = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
        )
        if state.is_paused and is_cleaning_state:
            return VacuumActivity.PAUSED
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_charging_to_resume:
            return VacuumActivity.CLEANING
        if _is_dock_side(state):
            return VacuumActivity.DOCKED
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity is not None:
            return activity
        # Unknown working_status value — infer from dock signals so we
        # don't report IDLE while the robot is clearly active off-dock.
        # New firmware versions may introduce values we haven't mapped yet.
        if not state.is_docked:
            if state.working_status.value not in _WARNED_UNMAPPED_ACTIVITY:
                _WARNED_UNMAPPED_ACTIVITY.add(state.working_status.value)
                _LOGGER.warning(
                    "Unmapped working_status %s (%d) while off-dock — reporting "
                    "CLEANING (further occurrences of this value are suppressed)",
                    state.working_status.name, state.working_status.value,
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return task context for dashboard cards and automations."""
        state = self.coordinator.data
        if state is None:
            return None

        attributes: dict[str, Any] = {
            "task_status": _task_status(state),
            "busy": is_narwal_task_busy(state),
            "setup_available": is_setup_available(state),
            "working_status": state.working_status.name.lower(),
            "battery_level": state.battery_level,
            "charging_state": _charging_state(state),
            "charging_to_resume": state.is_charging_to_resume,
            "docked": _is_dock_side(state),
        }
        room_names = _room_names_by_id(state)
        if room_names:
            attributes["rooms"] = [
                {"id": room_id, "name": name}
                for room_id, name in sorted(room_names.items(), key=lambda item: item[1].lower())
            ]
        station_task = dock_task(state)
        if station_task is not None:
            attributes["station_task"] = station_task
        dock_remaining = (
            state.dock_drying_remaining_time
            if state.dock_drying_remaining_time is not None
            else state.dry_mop_remaining_time
        )
        if dock_remaining is not None:
            attributes["drying_time_left"] = dock_remaining
            attributes["drying_time_left_minutes"] = _duration_minutes(dock_remaining)
            attributes["dock_time_left"] = _format_duration(dock_remaining)
        dock_progress = state.dock_drying_progress_percent
        if dock_progress is None and state.mop_drying_target > 0:
            dock_progress = min(
                100,
                round(state.mop_drying_elapsed / state.mop_drying_target * 100),
            )
        if dock_progress is not None:
            attributes["dock_progress"] = dock_progress
            attributes["dock_progress_display"] = f"{dock_progress}%"
        if state.dock_drying_timer_fields is not None:
            attributes["dock_timer_fields"] = "/".join(state.dock_drying_timer_fields)
        active_cleaning_metrics = _has_active_cleaning_metrics(state)
        active_room_plan = _has_active_room_plan(state)
        if active_cleaning_metrics and state.current_room_id is not None:
            attributes["current_room_id"] = state.current_room_id
        if active_cleaning_metrics and state.task_progress_percent is not None:
            attributes["progress"] = state.task_progress_percent
        if active_cleaning_metrics and state.task_remaining_time > 0:
            attributes["remaining_time"] = state.task_remaining_time
        if active_cleaning_metrics and state.current_room_name:
            attributes["current_room"] = state.current_room_name
        active_room_ids = getattr(self.coordinator, "active_room_ids", None)
        if not isinstance(active_room_ids, list):
            active_room_ids = None
        if active_room_plan and active_room_ids:
            attributes["active_room_ids"] = active_room_ids
            attributes["active_segments"] = [str(room_id) for room_id in active_room_ids]
            attributes["active_rooms"] = [
                room_names.get(room_id, str(room_id)) for room_id in active_room_ids
            ]
        elif active_cleaning_metrics and state.current_room_id is not None:
            attributes["active_room_ids"] = [state.current_room_id]
            attributes["active_segments"] = [str(state.current_room_id)]
            attributes["active_rooms"] = [
                state.current_room_name or str(state.current_room_id)
            ]
        if active_cleaning_metrics and state.cleaning_area > 0:
            attributes["cleaning_area"] = round(state.cleaning_area, 2)
        if active_cleaning_metrics and state.cleaning_time > 0:
            attributes["cleaning_time"] = state.cleaning_time
        return attributes

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

    async def _state_after_wake(self):
        """Wake the robot and return the freshest client state."""
        await self._ensure_awake()
        return self.coordinator.client.state

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        state = await self._state_after_wake()
        if can_resume_cleaning(state):
            resp = await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
            _raise_if_command_failed(resp, "resume")
            return
        if not can_start_cleaning(state):
            raise HomeAssistantError("Narwal clean cannot be started right now")

        # Whole-house clean enumerates every room via clean/start_clean, matching the
        # app's allRoomIds() path. clean/plan/start (StartWithPlan) would instead re-run
        # the saved current plan — i.e. the last room selection — not the whole house.
        room_ids = await self._all_room_ids()
        if not can_start_cleaning(self.coordinator.client.state):
            raise HomeAssistantError("Narwal clean cannot be started right now")
        if room_ids:
            settings = self.coordinator.clean_settings
            resp = await self.coordinator.client.start_rooms(
                room_ids,
                work_mode=settings.work_mode,
                fan=settings.fan,
                water=settings.water,
                mop_strength=settings.mop_strength,
                passes=settings.passes,
                route=settings.route,
                room_settings=self.coordinator.room_clean_settings_for_rooms(room_ids),
            )
        else:
            # No map rooms known — best-effort fall back to the saved-plan start.
            resp = await self.coordinator.client.start()
        _LOGGER.info(
            "Whole-house start: code=%s, success=%s, rooms=%s",
            resp.result_code, resp.success, room_ids or "(saved plan)",
        )
        if not resp.success:
            _LOGGER.warning("Start command failed: %s", _result_name(resp.result_code))
        _raise_if_command_failed(resp, "start")
        if room_ids:
            self.coordinator.set_active_room_ids(room_ids)
            self.async_write_ha_state()

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
        state = await self._state_after_wake()
        if not can_stop_cleaning(state):
            raise HomeAssistantError("Narwal clean cannot be stopped right now")
        resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop response: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "stop")
        if resp.success:
            self.coordinator.set_active_room_ids(None)
            self.async_write_ha_state()

    async def async_pause(self) -> None:
        """Pause cleaning."""
        state = await self._state_after_wake()
        if not can_pause_cleaning(state):
            raise HomeAssistantError("Narwal clean cannot be paused right now")
        resp = await self.coordinator.client.pause()
        _LOGGER.info("Pause response: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "pause")

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        state = await self._state_after_wake()
        if not can_return_home(state):
            raise HomeAssistantError("Narwal cannot return to the dock right now")
        resp = await self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Return-to-base response: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        if not resp.success:
            _LOGGER.warning(
                "Return-to-base did not succeed (code=%s)", resp.result_code,
            )
        _raise_if_command_failed(resp, "return to dock")
        self.async_write_ha_state()

    async def async_locate(self, **kwargs) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        state = await self._state_after_wake()
        if not can_locate_robot(state):
            raise HomeAssistantError("Narwal locate cannot be used right now")
        resp = await self.coordinator.client.locate()
        _raise_if_command_failed(resp, "locate")

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Set the fan speed.

        Stores it as the pending suction for the next clean; if the robot is
        currently cleaning, also writes it live via set_fan_speed.
        """
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            return
        if not self.available:
            raise HomeAssistantError("Narwal fan speed cannot be changed right now")
        state = self.coordinator.data
        if not clean_setting_applies_to_mode("fan", self.coordinator.clean_settings.work_mode):
            raise HomeAssistantError(
                "Narwal fan speed is not available in mop-only mode"
            )
        if not (
            can_edit_pending_clean_settings(state)
            or is_live_clean_setting_available(state)
        ):
            raise HomeAssistantError("Narwal fan speed cannot be changed right now")
        if is_live_clean_setting_available(state):
            resp = await self.coordinator.client.set_fan_speed(level)
            _raise_if_command_failed(resp, "set fan speed")
        self.coordinator.clean_settings.fan = level
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

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
        try:
            room_ids = [int(sid) for sid in segment_ids]
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal segment IDs must be numeric") from err
        if not room_ids:
            raise HomeAssistantError("Narwal segment IDs must not be empty")
        if any(room_id <= 0 for room_id in room_ids):
            raise HomeAssistantError("Narwal segment IDs must be positive")
        state = await self._state_after_wake()
        if not can_start_cleaning(state):
            raise HomeAssistantError("Narwal room clean cannot be started right now")
        state = self.coordinator.data
        known_ids: set[int] = set()
        if state is None or state.map_data is None:
            try:
                await self.coordinator.client.get_map()
            except Exception:
                _LOGGER.debug("Could not fetch Narwal map before segment clean")
            state = self.coordinator.data
        if state is not None and state.map_data is not None:
            known_ids = {room.room_id for room in state.map_data.rooms if room.room_id > 0}
        else:
            known_ids = {
                int(segment.id)
                for segment in (getattr(self, "last_seen_segments", None) or [])
                if str(segment.id).isdigit() and int(segment.id) > 0
            }
        if not known_ids:
            raise HomeAssistantError("Narwal map is not available")
        unknown_ids = [room_id for room_id in room_ids if room_id not in known_ids]
        if unknown_ids:
            raise HomeAssistantError(
                f"Unknown Narwal room ID: {', '.join(str(room_id) for room_id in unknown_ids)}"
            )
        if not can_start_cleaning(self.coordinator.client.state):
            raise HomeAssistantError("Narwal room clean cannot be started right now")
        settings = self.coordinator.clean_settings
        _LOGGER.info(
            "Starting room-specific clean: rooms=%s mode=%s fan=%s water=%s "
            "mop_strength=%s passes=%s route=%s",
            room_ids, settings.work_mode.name, settings.fan.name,
            settings.water.name, settings.mop_strength.name, settings.passes,
            settings.route.name,
        )
        resp = await self.coordinator.client.start_rooms(
            room_ids,
            work_mode=settings.work_mode,
            fan=settings.fan,
            water=settings.water,
            mop_strength=settings.mop_strength,
            passes=settings.passes,
            route=settings.route,
            room_settings=self.coordinator.room_clean_settings_for_rooms(room_ids),
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
        _raise_if_command_failed(resp, "room clean")
        self.coordinator.set_active_room_ids(room_ids)
        self.async_write_ha_state()

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
        if current_set == last_set:
            self._last_segment_change_signature = None
            return

        signature = (tuple(sorted(last_set)), tuple(sorted(current_set)))
        if signature != getattr(self, "_last_segment_change_signature", None):
            self._last_segment_change_signature = signature
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set), len(current_set),
            )
            self.async_create_segments_issue()
