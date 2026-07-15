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

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalState, WorkingStatus


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | str | None]


def _has_active_cleaning_metrics(state: NarwalState) -> bool:
    return state.is_cleaning or state.has_recent_active_working_status


def _station_task(state: NarwalState) -> str | None:
    """Return the active dock task."""
    if not state.is_station_active:
        return None
    if state.station_activity == 1:
        return "emptying_dustbin"
    if state.station_activity in (2, 3):
        return "washing_mop"
    if state.dry_mop_remaining_time is not None and state.dry_mop_remaining_time > 0:
        return "drying_mop"
    if state.station_activity == 4:
        return "drying_or_disinfecting"
    return "station_active"


SENSOR_DESCRIPTIONS: tuple[NarwalSensorEntityDescription, ...] = (
    NarwalSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # battery_level comes from field 2 (real-time SOC as float32)
        value_fn=lambda state: state.battery_level if state.battery_level > 0 else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: round(state.cleaning_area / 10000, 2)
        if state.cleaning_area > 0 and _has_active_cleaning_metrics(state)
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
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
    ),
    NarwalSensorEntityDescription(
        key="current_room",
        translation_key="current_room",
        value_fn=lambda state: state.current_room_name
        if state.current_room_name and _has_active_cleaning_metrics(state)
        else None,
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
    ),
    NarwalSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version or None,
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
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

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
        is_cleaning_status = state.working_status in (
            WorkingStatus.CLEANING,
            WorkingStatus.CLEANING_V2,
            WorkingStatus.CLEANING_ALT,
            WorkingStatus.CLEANING_FLOW2,
        ) or state.has_recent_active_working_status
        if state.working_status == WorkingStatus.ERROR:
            return "error"
        if state.task_active and (state.task_paused or state.is_paused):
            return "paused"
        if state.is_returning:
            return "returning"
        if state.is_station_active:
            return "station_active"
        if state.task_active:
            return "cleaning"
        if state.is_docked:
            return "docked"
        if state.working_status == WorkingStatus.TASK_COMPLETED:
            return "returning"
        if state.is_paused and is_cleaning_status:
            return "paused"
        if state.is_cleaning:
            return "cleaning"
        if state.working_status == WorkingStatus.STANDBY:
            return "idle"
        return "unknown"

    @property
    def icon(self) -> str:
        """Return icon based on task status."""
        value = self.native_value
        if value == "station_active":
            return "mdi:home-automation"
        if value == "cleaning":
            return "mdi:robot-vacuum"
        if value == "returning":
            return "mdi:home-import-outline"
        if value == "paused":
            return "mdi:pause"
        if value == "error":
            return "mdi:alert-circle-outline"
        return "mdi:information-outline"
