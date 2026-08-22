"""Binary sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from .narwal_client import NarwalState

from . import NarwalConfigEntry
from .cloud import NarwalCloudConsumable
from .const import (
    CONSUMABLE_MAINTAIN_ITEMS,
    CONSUMABLE_REPLACE_ITEMS,
    ERROR_HELP_URL_TEMPLATE,
)
from .coordinator import NarwalCoordinator, is_narwal_task_busy, is_setup_available
from .entity import NarwalEntity, is_dock_consumable_identity


@dataclass(frozen=True, kw_only=True)
class NarwalBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Narwal binary sensor; value_fn returns None when unavailable."""

    value_fn: Callable[[NarwalState], bool | None]
    attrs_fn: Callable[[NarwalState], dict[str, Any] | None] | None = None
    dock_device: bool = False


def _tank_problem(attr: str, bad: frozenset[int]) -> Callable[[NarwalState], bool | None]:
    """A station tank/bag state is a problem when its enum value is one of `bad`.

    The state attr is None when this model doesn't report that field, which
    keeps the entity unavailable rather than asserting "OK".
    """
    def fn(state: NarwalState) -> bool | None:
        value = getattr(state, attr)
        return None if value is None else value in bad
    return fn


def _is_dock_side(state: NarwalState) -> bool:
    """Return true when the robot or dock is doing dock-side work."""
    return state.is_docked or state.is_charging_to_resume or state.is_station_active


# Station tank/bag problem sensors. Bad-value sets come from the decoded enums
# (RobotBaseStatus.pbenum): every named value ≥ 2 is an attention state
# (empty / abnormal / not-installed / suggest-replace); 0=unspecified, 1=ok.
BINARY_SENSOR_DESCRIPTIONS: tuple[NarwalBinarySensorEntityDescription, ...] = (
    NarwalBinarySensorEntityDescription(
        key="busy",
        translation_key="busy",
        value_fn=is_narwal_task_busy,
    ),
    NarwalBinarySensorEntityDescription(
        key="setup_available",
        translation_key="setup_available",
        value_fn=is_setup_available,
    ),
    NarwalBinarySensorEntityDescription(
        key="error",
        translation_key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # base_status field 1 errorCode: empty when healthy, populated on a fault.
        value_fn=lambda s: s.has_error if s.raw_base_status else None,
        # Expose the fault detail (numeric code(s), severity, debug string, help link) when present.
        attrs_fn=lambda s: {
            "codes": s.error_codes,
            "level": s.error_level,
            "detail": s.error_detail,
            **(
                {"help_url": ERROR_HELP_URL_TEMPLATE.format(code=s.error_codes[0])}
                if s.error_codes else {}
            ),
        } if s.raw_base_status else None,
    ),
    NarwalBinarySensorEntityDescription(
        key="maintenance_required",
        translation_key="maintenance_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # consumable/get_consumable_info maintainItems (clean/check these parts).
        value_fn=lambda s: bool(s.maintain_items)
        if s.consumable_info_available
        else None,
        attrs_fn=lambda s: {
            "items": [CONSUMABLE_MAINTAIN_ITEMS.get(i, str(i)) for i in s.maintain_items]
        } if s.consumable_info_available else None,
    ),
    NarwalBinarySensorEntityDescription(
        key="replacement_required",
        translation_key="replacement_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # consumable/get_consumable_info replaceItems (replace these parts).
        value_fn=lambda s: bool(s.replace_items)
        if s.consumable_info_available
        else None,
        attrs_fn=lambda s: {
            "items": [CONSUMABLE_REPLACE_ITEMS.get(i, str(i)) for i in s.replace_items]
        } if s.consumable_info_available else None,
    ),
    NarwalBinarySensorEntityDescription(
        key="clean_water_tank",
        translation_key="clean_water_tank",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        value_fn=_tank_problem("clean_water_tank_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="sewage_tank",
        translation_key="sewage_tank",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        value_fn=_tank_problem("sewage_tank_state", frozenset({2, 3})),
    ),
    NarwalBinarySensorEntityDescription(
        key="dust_box",
        translation_key="dust_box",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("dust_box_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="dust_bag",
        translation_key="dust_bag",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        value_fn=_tank_problem("dust_bag_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="station_bag",
        translation_key="station_bag",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        value_fn=_tank_problem("station_bag_state", frozenset({2, 3, 4})),
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
    entities += [
        NarwalBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)

    known_consumables: set[str] = set()

    @callback
    def async_add_consumables() -> None:
        new_consumables = sorted(
            (
                consumable
                for code, consumable in coordinator.cloud_consumables.items()
                if code not in known_consumables and consumable.has_overdue_signal
            ),
            key=lambda item: item.name.lower(),
        )
        if not new_consumables:
            return
        known_consumables.update(item.code for item in new_consumables)
        async_add_entities(
            NarwalConsumableOverdueBinarySensor(coordinator, consumable)
            for consumable in new_consumables
        )

    async_add_consumables()
    entry.async_on_unload(coordinator.async_add_listener(async_add_consumables))


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
        if getattr(state, "dock_state_unknown", False):
            return None
        return _is_dock_side(state)


class NarwalBinarySensor(NarwalEntity, BinarySensorEntity):
    """A description-driven Narwal binary sensor (fault / station consumables)."""

    entity_description: NarwalBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        if description.dock_device:
            self._use_dock_device_info()
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the sensor value (None = unavailable)."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def available(self) -> bool:
        """Return False when this model has not reported the backing field."""
        if not super().available:
            return False
        state = self.coordinator.data
        return state is not None and self.entity_description.value_fn(state) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Optional per-sensor attributes (e.g. fault code detail)."""
        state = self.coordinator.data
        if state is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(state)


class NarwalConsumableOverdueBinarySensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor for an overdue cloud consumable."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        consumable: NarwalCloudConsumable,
    ) -> None:
        """Initialize the consumable overdue sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._consumable_code = consumable.code
        self._attr_unique_id = (
            f"{device_id}_consumable_{slugify(consumable.code)}_overdue"
        )
        self._attr_name = f"{consumable.name} overdue"
        if is_dock_consumable_identity(consumable.code, consumable.name):
            self._use_dock_device_info()

    @property
    def is_on(self) -> bool | None:
        """Return True when the cloud consumable is overdue."""
        consumable = self.coordinator.cloud_consumables.get(self._consumable_code)
        if consumable is None or not consumable.has_overdue_signal:
            return None
        return consumable.is_overdue

    @property
    def available(self) -> bool:
        """Return true when the latest cloud consumable state is current."""
        return (
            self.coordinator.cloud_consumables_error is None
            and (
                consumable := self.coordinator.cloud_consumables.get(
                    self._consumable_code
                )
            )
            is not None
            and consumable.has_overdue_signal
        )

    @property
    def extra_state_attributes(self) -> dict[str, int | float | str | bool] | None:
        """Return diagnostic consumable details."""
        consumable = self.coordinator.cloud_consumables.get(self._consumable_code)
        if consumable is None:
            return None
        return {
            "consumables_code": consumable.code,
            "used_hours": consumable.used_hours,
            "total_hours": consumable.total_hours,
            "remaining_hours": consumable.remaining_hours,
            "used_percent": consumable.used_percent,
            "remaining_percent": consumable.remaining_percent,
        }
