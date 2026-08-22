"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .narwal_client import NarwalState, WorkingStatus
from .narwal_client.const import ACTIVE_CLEANING_STATUSES

from . import NarwalConfigEntry
from .const import TASK_RESULT_OPTIONS
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | int | str | None]
    available_fn: Callable[[NarwalState], bool] | None = None


def _has_active_cleaning_metrics(state: NarwalState) -> bool:
    return (
        state.is_cleaning
        or state.has_recent_active_working_status
        or state.is_paused
    )


def _station_task(state: NarwalState) -> str | None:
    """Return the active dock task."""
    if not state.is_station_active:
        return "idle"
    if state.station_activity == 1:
        return "emptying_dustbin"
    if state.station_activity in (2, 3):
        return "washing_mop"
    if state.dry_mop_remaining_time is not None and state.dry_mop_remaining_time > 0:
        return "drying_mop"
    if state.station_activity == 4:
        return "drying_or_disinfecting"
    return "station_active"


def _has_base_status_field(state: NarwalState, field: str) -> bool:
    """Return whether the latest base-status payload contains a field."""
    return isinstance(state.raw_base_status, dict) and field in state.raw_base_status


SENSOR_DESCRIPTIONS: tuple[NarwalSensorEntityDescription, ...] = (
    NarwalSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # battery_level comes from field 2 (real-time SOC as float32)
        value_fn=lambda state: state.battery_level
        if _has_base_status_field(state, "2")
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 2 (coveredArea) is already m²; populated only during active cleaning.
        value_fn=lambda state: round(state.cleaning_area, 2)
        if state.cleaning_area > 0 and _has_active_cleaning_metrics(state)
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 3 is session elapsed seconds.
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: state.cleaning_time
        if state.cleaning_time > 0 and _has_active_cleaning_metrics(state)
        else None,
    ),
    NarwalSensorEntityDescription(
        key="task_progress",
        translation_key="task_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.task_progress_percent
        if state.task_progress_percent is not None and _has_active_cleaning_metrics(state)
        else None,
        available_fn=lambda state: (
            state.task_progress_percent is not None
            and _has_active_cleaning_metrics(state)
        ),
    ),
    NarwalSensorEntityDescription(
        key="dust_bag_health",
        translation_key="dust_bag_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # base_status field 35 stationBagHealthScore (float32 %); present only with a station.
        value_fn=lambda state: round(state.dust_bag_health, 1)
        if _has_base_status_field(state, "35")
        else None,
    ),
    NarwalSensorEntityDescription(
        key="detergent_remaining",
        translation_key="detergent_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # base_status field 41 heavyDetergentRemainPercent.
        value_fn=lambda state: state.detergent_remaining
        if _has_base_status_field(state, "41")
        else None,
    ),
    NarwalSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version or None,
    ),
    NarwalSensorEntityDescription(
        key="last_clean_result",
        translation_key="last_clean_result",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=list(TASK_RESULT_OPTIONS.values()),
        # base_status field 15 terminateReason (TaskResult) — why the last task ended.
        value_fn=lambda state: TASK_RESULT_OPTIONS.get(state.terminate_reason),
    ),
    NarwalSensorEntityDescription(
        key="current_room",
        translation_key="current_room",
        icon="mdi:map-marker",
        # working_status field 6: room_id of the room currently being cleaned.
        # Resolved to a display name via the cached room map from get_map.
        value_fn=lambda state: state.current_room_name,
        available_fn=lambda state: (
            state.current_room_name is not None and _has_active_cleaning_metrics(state)
        ),
    ),
    NarwalSensorEntityDescription(
        key="station_task",
        translation_key="station_task",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "emptying_dustbin",
            "washing_mop",
            "drying_mop",
            "drying_or_disinfecting",
            "station_active",
            "idle",
        ],
        value_fn=_station_task,
    ),
    NarwalSensorEntityDescription(
        key="dry_mop_remaining_time",
        translation_key="dry_mop_remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.dry_mop_remaining_time
        if state.is_station_active
        and state.dry_mop_remaining_time is not None
        and state.dry_mop_remaining_time > 0
        else None,
        available_fn=lambda state: (
            state.is_station_active
            and state.dry_mop_remaining_time is not None
            and state.dry_mop_remaining_time > 0
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NarwalSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(NarwalChargingStateSensor(coordinator))
    entities.append(NarwalTaskStatusSensor(coordinator))
    async_add_entities(entities)


class NarwalSensor(NarwalEntity, SensorEntity):
    """A Narwal sensor entity."""

    entity_description: NarwalSensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def available(self) -> bool:
        """Return False only for sensors with transient backing data."""
        if not super().available:
            return False
        state = self.coordinator.data
        if state is None:
            return False
        if self.entity_description.available_fn is None:
            return True
        return self.entity_description.available_fn(state)


class NarwalChargingStateSensor(NarwalEntity, SensorEntity):
    """Sensor showing charging state: Charging, Fully Charged, or unavailable."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "charging_state"
    _attr_options = ["charging", "fully_charged", "not_charging"]

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the charging state sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_charging_state"

    @property
    def native_value(self) -> str | None:
        """Return charging state.

        Returns None (unavailable) when not docked.
        """
        state = self.coordinator.data
        if state is None:
            return None
        if not state.is_docked:
            return "not_charging"
        if state.battery_level >= 100:
            return "fully_charged"
        return "charging"

    @property
    def icon(self) -> str:
        """Return icon based on charging state."""
        if self.native_value == "fully_charged":
            return "mdi:battery"
        if self.native_value == "charging":
            return "mdi:battery-charging"
        if self.native_value == "not_charging":
            return "mdi:battery-off-outline"
        return "mdi:battery-unknown"


class NarwalTaskStatusSensor(NarwalEntity, SensorEntity):
    """Sensor showing the active cleaning or dock task state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "task_status"
    _attr_options = [
        "cleaning",
        "remapping",
        "returning",
        "paused",
        "station_active",
        "docked",
        "idle",
        "error",
        "unknown",
    ]

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the task status sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_task_status"

    @property
    def native_value(self) -> str | None:
        """Return the active task status."""
        state = self.coordinator.data
        if state is None:
            return None
        is_cleaning_status = (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
        )
        if state.working_status == WorkingStatus.ERROR or state.has_error:
            return "error"
        if state.working_status == WorkingStatus.REMAPPING:
            return "remapping"
        if state.is_paused and is_cleaning_status:
            return "paused"
        if state.is_returning:
            return "returning"
        if state.is_station_active:
            return "station_active"
        if state.is_cleaning:
            return "cleaning"
        if state.is_docked:
            return "docked"
        if state.working_status == WorkingStatus.STANDBY:
            return "idle"
        return "unknown"

    @property
    def icon(self) -> str:
        """Return icon based on task status."""
        value = self.native_value
        if value == "station_active":
            return "mdi:home-automation"
        if value == "remapping":
            return "mdi:map-sync-outline"
        if value == "cleaning":
            return "mdi:robot-vacuum"
        if value == "returning":
            return "mdi:home-import-outline"
        if value == "paused":
            return "mdi:pause"
        if value == "error":
            return "mdi:alert-circle-outline"
        return "mdi:information-outline"
