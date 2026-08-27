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
from .const import (
    fan_speed_label_map_for,
    fan_speed_list_for,
    fan_speed_map_for,
)
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
    is_live_clean_setting_available,
)
from .dock_tasks import (
    can_start_robot_clean,
    can_stop_dock_task,
    dock_task_blocks_robot_return,
    is_clean_session_context,
)
from .entity import NarwalEntity
from .narwal_client import CommandResult, FanLevel, WorkingStatus
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

_LOGGER = logging.getLogger(__name__)

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


def _raise_if_command_failed(response: Any, action: str) -> None:
    """Raise a Home Assistant service error for rejected robot commands."""
    if response.accepted:
        return
    raise HomeAssistantError(
        f"Narwal {action} failed: {_result_name(response.result_code)}"
    )


def _task_status(state: Any) -> str:
    """Return a compact active-task status for dashboards and automations."""
    is_cleaning_state = (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
    )
    if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
        return "error"
    if state.working_status == WorkingStatus.REMAPPING:
        return "remapping"
    if state.is_paused and is_cleaning_state:
        return "paused"
    if state.is_returning:
        return "returning"
    if state.is_cleaning:
        return "cleaning"
    if state.is_station_active:
        return "station_active"
    if state.is_docked:
        return "docked"
    if state.working_status == WorkingStatus.STANDBY:
        return "idle"
    return "unknown"


def _is_dock_side(state: Any) -> bool:
    """Return true when robot telemetry says it is physically dock-side."""
    return state.is_docked


def _has_active_cleaning_metrics(state: Any) -> bool:
    """Return true while live clean-progress details are current."""
    return (
        state.is_cleaning
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
    )


def _has_dock_stop_context(state: Any) -> bool:
    """Return true when dock-side work must be considered before generic stop."""
    return (
        state.is_station_active
        or state.has_unmapped_active_dock_task
        or bool(state.active_dock_task_keys)
    )


def _can_stop_vacuum(state: Any) -> bool:
    """Return true when the aggregate vacuum stop command is safe to expose."""
    if _has_live_robot_stop_context(state):
        return can_stop_cleaning(state) and not dock_task_blocks_robot_return(state)
    if _has_dock_stop_context(state):
        return False
    return can_stop_cleaning(state)


def _has_live_robot_stop_context(state: Any) -> bool:
    """Return true when a robot-side task is actively stoppable, not just retained."""
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or state.has_paused_clean_task_context
        or state.is_returning
    )


def _status_summary(state: Any) -> str:
    """Return one concise status line for HA tile state content."""
    status = _task_status(state)
    active_cleaning_metrics = _has_active_cleaning_metrics(state)
    parts: list[str] = []

    if active_cleaning_metrics and state.current_room_name:
        parts.append(state.current_room_name)
    if active_cleaning_metrics and state.task_progress_percent is not None:
        parts.append(f"{state.task_progress_percent}%")
    if parts:
        return " - ".join(parts)

    return status.replace("_", " ").title()


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

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["device_id"]
        # Offered tiers are per-model: models whose app tops out at DEEP don't get "Ultra".
        self._attr_fan_speed_list = fan_speed_list_for(coordinator.config_entry.data)
        self._last_reported_segment_signature = None

    async def async_added_to_hass(self) -> None:
        """Restore the pending fan speed into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or "fan_speed" not in last.attributes:
            return
        fan = last.attributes.get("fan_speed")
        if fan is None:
            self.coordinator.clean_settings.fan = FanLevel.UNSPECIFIED
            return
        fan_map = fan_speed_map_for(self.coordinator.config_entry.data)
        if fan in fan_map:
            self.coordinator.clean_settings.fan = fan_map[fan]

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Return currently usable native Home Assistant vacuum features."""
        features = VacuumEntityFeature.STATE
        state = self.coordinator.data
        if state is None or not self.available:
            return features

        if can_start_cleaning(state) or can_resume_cleaning(state):
            features |= VacuumEntityFeature.START
        if _can_stop_vacuum(state):
            features |= VacuumEntityFeature.STOP
        if can_pause_cleaning(state):
            features |= VacuumEntityFeature.PAUSE
        if can_return_home(state):
            features |= VacuumEntityFeature.RETURN_HOME
        if can_locate_robot(state):
            features |= VacuumEntityFeature.LOCATE
        if self._fan_speed_available(state):
            features |= VacuumEntityFeature.FAN_SPEED
        if Segment is not None and can_start_cleaning(state):
            features |= VacuumEntityFeature.CLEAN_AREA
        return features

    def _fan_speed_available(self, state: Any) -> bool:
        """Return True when HA should expose the native fan speed control."""
        setup_available = can_edit_pending_clean_settings(state)
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_settings.work_mode,
        )
        live_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_setting_applicability_mode(live=True),
        )
        return (setup_available and setup_applies) or (
            live_available and live_applies
        )

    @property
    def activity(self) -> VacuumActivity:
        """Return the current vacuum activity."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        is_cleaning_state = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
            or state.has_paused_clean_task_context
        )
        if state.working_status == WorkingStatus.ERROR or getattr(state, "has_error", False):
            return VacuumActivity.ERROR
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
        if _is_dock_side(state):
            return VacuumActivity.DOCKED
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity == VacuumActivity.DOCKED:
            return VacuumActivity.IDLE
        if activity is not None:
            return activity
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the selected fan speed.

        The robot does not broadcast the active fan level, so this reflects the
        pending value held in coordinator.clean_settings (applied at the next clean
        and, while cleaning, written live via set_fan_speed).
        """
        return fan_speed_label_map_for(self.coordinator.config_entry.data).get(
            self.coordinator.clean_settings.fan
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return task context for dashboard cards and automations."""
        state = self.coordinator.data
        if state is None:
            return None

        attributes: dict[str, Any] = {
            "task_status": _task_status(state),
            "status_summary": _status_summary(state),
        }
        active_cleaning_metrics = _has_active_cleaning_metrics(state)
        if active_cleaning_metrics and state.task_progress_percent is not None:
            attributes["progress"] = state.task_progress_percent
        if active_cleaning_metrics and state.current_room_name:
            attributes["current_room"] = state.current_room_name
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

    async def _validate_clean_start(self) -> None:
        """Refresh dock state and reject starts that conflict with dock work."""
        if not await self.coordinator.async_refresh_dock_status():
            raise HomeAssistantError("Narwal status could not be refreshed")
        if not can_start_robot_clean(self.coordinator.client.state):
            raise HomeAssistantError("Narwal dock task is active")

    async def _state_after_wake(self):
        """Wake the robot and return the freshest client state."""
        await self._ensure_awake()
        return self.coordinator.client.state

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        room_ids: list[int] = []
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            await self.coordinator.async_refresh_action_status()
            state = self.coordinator.client.state
            if can_resume_cleaning(state):
                resp = await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
                _raise_if_command_failed(resp, "resume")
                return
            await self._validate_clean_start()
            if not can_start_cleaning(self.coordinator.client.state):
                raise HomeAssistantError("Narwal clean cannot be started right now")

            # Whole-house clean enumerates every room via clean/start_clean, matching the
            # app's allRoomIds() path. clean/plan/start (StartWithPlan) would instead re-run
            # the saved current plan — i.e. the last room selection — not the whole house.
            room_ids = await self._all_room_ids()
            if not room_ids:
                raise HomeAssistantError("Narwal room map is not available")
            settings = self.coordinator.clean_settings
            room_settings = self.coordinator.room_clean_settings_for_rooms(room_ids)
            try:
                self.coordinator.compatible_room_clean_work_mode(room_settings)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            resp = await self.coordinator.client.start_rooms(
                room_ids,
                work_mode=settings.work_mode,
                fan=settings.fan,
                water=settings.water,
                mop_strength=settings.mop_strength,
                passes=settings.passes,
                route=settings.route,
                room_settings=room_settings,
            )
            if resp.accepted:
                self.coordinator.record_accepted_clean_start(room_settings)
                self.coordinator.client.state.assume_robot_clean()
                self.coordinator.async_set_updated_data(self.coordinator.client.state)
        _LOGGER.info(
            "Whole-house start: code=%s, success=%s, rooms=%s",
            resp.result_code, resp.success, room_ids,
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
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            refreshed = await self.coordinator.async_refresh_action_status()
            state = self.coordinator.client.state
            clean_context = is_clean_session_context(state)
            if state.has_unmapped_active_dock_task:
                raise HomeAssistantError(
                    "Narwal dock task cannot be stopped safely right now"
                )
            if (
                _has_live_robot_stop_context(state)
                and can_stop_cleaning(state)
                and not dock_task_blocks_robot_return(state)
            ):
                resp = await self.coordinator.client.stop()
            elif _has_dock_stop_context(state):
                if _has_live_robot_stop_context(state):
                    raise HomeAssistantError(
                        "Narwal dock task cannot be stopped safely right now"
                    )
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
                if not can_stop_cleaning(state):
                    raise HomeAssistantError("Narwal clean cannot be stopped right now")
                resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop response: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "stop")

    async def async_pause(self) -> None:
        """Pause cleaning."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            await self.coordinator.async_refresh_action_status()
            state = self.coordinator.client.state
            if not can_pause_cleaning(state):
                raise HomeAssistantError("Narwal clean cannot be paused right now")
            resp = await self.coordinator.client.pause()
        _LOGGER.info("Pause response: code=%s, success=%s", resp.result_code, resp.success)
        _raise_if_command_failed(resp, "pause")

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        async with self.coordinator.dock_action_lock:
            await self._ensure_awake()
            await self.coordinator.async_refresh_action_status()
            state = self.coordinator.client.state
            if not can_return_home(state):
                raise HomeAssistantError("Narwal cannot return to the dock right now")
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
        fan_speed_map = fan_speed_map_for(self.coordinator.config_entry.data)
        level = fan_speed_map.get(fan_speed)
        if level is None:
            raise HomeAssistantError(f"Unsupported Narwal fan speed: {fan_speed}")
        state = self.coordinator.data
        setup_available = can_edit_pending_clean_settings(state)
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_settings.work_mode,
        )
        live_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_setting_applicability_mode(live=True),
        )
        if not (
            (setup_available and setup_applies)
            or (live_available and live_applies)
        ):
            if not setup_applies and not live_applies:
                raise HomeAssistantError(
                    "Narwal fan speed is not available in mop-only mode"
                )
            raise HomeAssistantError("Narwal fan speed cannot be changed right now")
        if live_available and not live_applies:
            raise HomeAssistantError(
                "Narwal fan speed is not available in mop-only mode"
            )
        if live_available:
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
        await self._ensure_awake()
        try:
            room_ids = [int(sid) for sid in segment_ids]
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal segment IDs must be numeric") from err
        if not room_ids:
            raise HomeAssistantError("Narwal segment IDs must not be empty")
        if any(room_id <= 0 for room_id in room_ids):
            raise HomeAssistantError("Narwal segment IDs must be positive")

        async with self.coordinator.dock_action_lock:
            await self._validate_clean_start()
            state = self.coordinator.data
            known_ids: set[int] = set()
            if state is None or state.map_data is None:
                try:
                    await self.coordinator.client.get_map()
                except Exception:
                    _LOGGER.debug("Could not fetch Narwal map before segment clean")
                state = self.coordinator.data
            if state is not None and state.map_data is not None:
                known_ids = {
                    room.room_id for room in state.map_data.rooms if room.room_id > 0
                }
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
                    "Unknown Narwal room ID: "
                    f"{', '.join(str(room_id) for room_id in unknown_ids)}"
                )
            settings = self.coordinator.clean_settings
            room_settings = self.coordinator.room_clean_settings_for_rooms(room_ids)
            try:
                self.coordinator.compatible_room_clean_work_mode(room_settings)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
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
                room_settings=room_settings,
            )
            if resp.accepted:
                self.coordinator.record_accepted_clean_start(room_settings)
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
            self._last_reported_segment_signature = None
            return
        signature = (frozenset(last_set), frozenset(current_set))
        if signature == self._last_reported_segment_signature:
            return
        self._last_reported_segment_signature = signature
        _LOGGER.info(
            "Segment change detected: %d -> %d rooms",
            len(last_set), len(current_set),
        )
        self.async_create_segments_issue()
