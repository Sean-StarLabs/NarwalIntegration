"""Binary sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import is_maintenance_alerts_supported
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import MAINTENANCE_COMPONENT_IDS, NarwalState


@dataclass(frozen=True, kw_only=True)
class NarwalProblemBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Narwal problem binary sensor."""

    is_on_fn: Callable[[NarwalState], bool]


PROBLEM_DESCRIPTIONS: tuple[NarwalProblemBinarySensorEntityDescription, ...] = (
    *(
        NarwalProblemBinarySensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
            is_on_fn=lambda state, alert_key=key: alert_key in state.maintenance_alerts,
        )
        for key in MAINTENANCE_COMPONENT_IDS
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal binary sensor entities."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [NarwalDockedSensor(coordinator)]
    device_info = coordinator.client.state.device_info
    if is_maintenance_alerts_supported(
        entry.data, device_info.product_key if device_info else None
    ):
        entities.extend(
            NarwalProblemBinarySensor(coordinator, description)
            for description in PROBLEM_DESCRIPTIONS
        )
    async_add_entities(entities)


class NarwalDockedSensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor that reports whether the vacuum is on the dock."""

    _attr_translation_key = "docked"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the docked sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_docked"

    @property
    def is_on(self) -> bool | None:
        """Return True if the vacuum is on the dock."""
        state = self.coordinator.data
        if state is None:
            return None
        return state.is_docked


class NarwalProblemBinarySensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor for a Narwal maintenance problem."""

    entity_description: NarwalProblemBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalProblemBinarySensorEntityDescription,
    ) -> None:
        """Initialize the maintenance problem sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return True when the maintenance problem is active."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.is_on_fn(state)
