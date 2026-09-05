"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import NarwalConfigEntry
from .const import (
    FAN_SPEED_LIST,
    FAN_SPEED_MAP,
    fan_speed_list_for,
    normalize_fan_level_for_model,
)
from .coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    clean_setting_applies_to_mode,
    has_blocking_error,
    is_clean_session_context,
    is_live_clean_setting_available,
)
from .dock_tasks import (
    can_start_robot_clean,
    can_stop_dock_task,
    has_active_dock_work,
)
from .entity import NarwalEntity
from .narwal_client import CommandResult, FanLevel, WorkingStatus
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

_LOGGER = logging.getLogger(__name__)

# working_status values already reported as unmapped. Activity is recomputed on
# every state broadcast, so warn once per distinct value instead of flooding.
_WARNED_UNMAPPED_ACTIVITY: set[int] = set()


@dataclass(frozen=True)
class FanSettingRestoreData(ExtraStoredData):
    """Persist pending suction by its stable robot value."""

    value: int
    version: int = 1

    def as_dict(self) -> dict[str, int]:
        """Return a JSON-serializable representation."""
        return {"version": self.version, "value": self.value}

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING_V2: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.CUSTOM_CLEANING: VacuumActivity.CLEANING,
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


def _raise_if_command_failed(response: Any, action: str) -> None:
    """Raise a Home Assistant service error for rejected robot commands."""
    if response.accepted:
        return
    raise HomeAssistantError(
        f"Narwal {action} failed: {_result_name(response.result_code)}"
    )


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
        setup_fan = (
            can_edit_pending_clean_settings(state)
            and not self.coordinator.has_selected_clean_rooms()
            and clean_setting_applies_to_mode(
                "fan", self.coordinator.clean_settings.work_mode
            )
        )
        live_fan = False
        if is_live_clean_setting_available(state):
            live_mode = self.coordinator.clean_setting_applicability_mode(live=True)
            live_fan = live_mode is not None and clean_setting_applies_to_mode(
                "fan", live_mode
            )
        if not setup_fan and not live_fan:
            features &= ~VacuumEntityFeature.FAN_SPEED
        if not getattr(self.coordinator, "_room_profile_store_loaded", True):
            features &= ~VacuumEntityFeature.CLEAN_AREA
            if not (state and state.is_paused and is_clean_session_context(state)):
                features &= ~VacuumEntityFeature.START
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
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            raw_value = data.get("value")
            values = {int(value): value for value in FanLevel}
            if (
                data.get("version") == 1
                and isinstance(raw_value, int)
                and not isinstance(raw_value, bool)
                and raw_value in values
            ):
                self.coordinator.clean_settings.fan = normalize_fan_level_for_model(
                    self.coordinator.config_entry.data,
                    values[raw_value],
                )
                return
        if last is None or "fan_speed" not in last.attributes:
            return
        fan = last.attributes.get("fan_speed")
        if fan is None:
            self.coordinator.clean_settings.fan = FanLevel.UNSPECIFIED
        elif fan in FAN_SPEED_MAP:
            self.coordinator.clean_settings.fan = normalize_fan_level_for_model(
                self.coordinator.config_entry.data,
                FAN_SPEED_MAP[fan],
            )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Return pending suction independently of its displayed active value."""
        return FanSettingRestoreData(value=int(self.coordinator.clean_settings.fan))

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
        fan = self.coordinator.active_clean_setting("fan")
        if fan is None:
            fan = self.coordinator.clean_settings.fan
        return _FAN_LABELS.get(int(fan))

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
        if (
            state
            and not has_blocking_error(state)
            and state.working_status != WorkingStatus.TASK_COMPLETED
            and state.is_paused
            and is_clean_session_context(state)
        ):
            response = await self.coordinator.client.resume(
                timeout=self._ACTION_TIMEOUT
            )
            _raise_if_command_failed(response, "resume")
            return
        if getattr(self.coordinator, "_room_profile_store_loaded", True) is False:
            raise HomeAssistantError("Narwal room profiles are not restored yet")
        room_ids: list[int] = []
        async with self.coordinator.dock_action_lock:
            await self._validate_clean_start()

            # The HA vacuum start command uses integration-owned room selections:
            # selected rooms when any are on, otherwise all rooms.
            all_room_ids = await self._all_room_ids()
            if not all_room_ids:
                raise HomeAssistantError("Narwal room map is not available")
            map_id = self.coordinator.room_settings_map_id(
                self.coordinator.client.state.map_data
            )
            if map_id is None and self.coordinator.selected_clean_rooms:
                raise HomeAssistantError("Narwal room selection is not available")
            room_ids = self.coordinator.selected_clean_room_ids_for(
                all_room_ids,
                map_id=map_id,
            )
            if not room_ids:
                raise HomeAssistantError("Narwal room selection is not available")
            settings = self.coordinator.clean_settings
            room_settings = self.coordinator.room_clean_settings_for_rooms(
                room_ids,
                map_id=map_id,
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
        _LOGGER.info(
            "Room-aware start: code=%s, success=%s, rooms=%s",
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
        """Return room ids from an authoritative static map."""
        try:
            await self.coordinator.client.get_map()
        except Exception as err:
            raise HomeAssistantError("Narwal map could not be refreshed") from err
        state = self.coordinator.client.state
        if state and state.map_data:
            return [r.room_id for r in state.map_data.rooms if r.room_id > 0]
        return []

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

        Stores it as whole-floor pending suction when no rooms are selected;
        while cleaning, it also writes the active task live.
        """
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            return
        state = self.coordinator.data
        has_selected_rooms = self.coordinator.has_selected_clean_rooms()
        setup_available = (
            can_edit_pending_clean_settings(state)
            and not has_selected_rooms
        )
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            "fan",
            self.coordinator.clean_settings.work_mode,
        )
        live_mode = self.coordinator.clean_setting_applicability_mode(live=True)
        live_applies = (
            live_mode is not None
            and clean_setting_applies_to_mode("fan", live_mode)
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
            self.coordinator.set_active_clean_setting("fan", level)
        if not has_selected_rooms:
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
        if getattr(self.coordinator, "_room_profile_store_loaded", True) is False:
            raise HomeAssistantError("Narwal room profiles are not restored yet")
        await self._ensure_awake()
        try:
            room_ids = list(dict.fromkeys(int(sid) for sid in segment_ids))
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal segment IDs must be numeric") from err
        if not room_ids:
            raise HomeAssistantError("Narwal segment IDs must not be empty")
        if any(room_id <= 0 for room_id in room_ids):
            raise HomeAssistantError("Narwal segment IDs must be positive")

        async with self.coordinator.dock_action_lock:
            await self._validate_clean_start()
            try:
                await self.coordinator.client.get_map()
            except Exception as err:
                raise HomeAssistantError(
                    "Narwal map could not be refreshed"
                ) from err
            state = self.coordinator.client.state
            known_ids = {
                room.room_id
                for room in getattr(getattr(state, "map_data", None), "rooms", ())
                if room.room_id > 0
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
        if current_set != last_set:
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set), len(current_set),
            )
            self.async_create_segments_issue()
