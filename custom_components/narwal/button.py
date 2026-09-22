"""Button entities for Narwal consumable maintenance actions."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .cloud import NarwalCloudConsumable, NarwalCloudError
from .const import CONSUMABLE_MAINTAIN_ITEMS, CONSUMABLE_REPLACE_ITEMS, DOMAIN
from .coordinator import NarwalCoordinator
from .entity import (
    NarwalEntity,
    is_dock_consumable_identity,
    is_dock_consumable_name,
)
from .narwal_client import CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalConsumableInfoResetDescription:
    """Description for clearing a robot-reported consumable alert item."""

    key: str
    suggested_key: str
    name: str
    icon: str
    maintain_items: tuple[int, ...] = ()
    replace_items: tuple[int, ...] = ()


CONSUMABLE_CLEAR_ICONS: dict[str, str] = {
    "anti-winding brush": "mdi:broom",
    "cliff sensor": "mdi:radar",
    "clear water filter": "mdi:air-filter",
    "dust bag": "mdi:bag-personal",
    "dust box": "mdi:delete-sweep",
    "dust container": "mdi:delete-sweep",
    "dust filter": "mdi:air-filter",
    "inner dust box": "mdi:delete-sweep",
    "mop": "mdi:roller-shade",
    "roller brush": "mdi:brush-variant",
    "side brush": "mdi:brush",
    "side distance sensor": "mdi:radar",
    "station bag": "mdi:delete-empty",
    "universal wheel": "mdi:cog-outline",
    "wash ribs": "mdi:squeegee",
    "water tank sponge": "mdi:sponge",
}

AUTOCLEAR_REPLACE_ITEMS = {
    "detergent",
    "heavy detergent",
}


def _friendly_item_name(item: str) -> str:
    """Return a short human label for a consumable alert item."""
    return item[:1].upper() + item[1:]


def _consumable_info_item_key(item: str) -> str:
    """Return the stable key fragment for a consumable alert item."""
    return slugify(item.replace("-", " "))


def _maintenance_reset_descriptions() -> tuple[NarwalConsumableInfoResetDescription, ...]:
    """Return reset button descriptions for robot consumable alert lists."""
    descriptions: list[NarwalConsumableInfoResetDescription] = []
    for code, item in sorted(CONSUMABLE_MAINTAIN_ITEMS.items()):
        item_key = _consumable_info_item_key(item)
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"maintenance_{item_key}_clear",
                suggested_key=f"{item_key}_maintenance_clear",
                name=f"{_friendly_item_name(item)} maintenance clear",
                icon=CONSUMABLE_CLEAR_ICONS.get(item, "mdi:wrench-clock"),
                maintain_items=(code,),
            )
        )
    for code, item in sorted(CONSUMABLE_REPLACE_ITEMS.items()):
        if item in AUTOCLEAR_REPLACE_ITEMS:
            continue
        item_key = _consumable_info_item_key(item)
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"replacement_{item_key}_clear",
                suggested_key=f"{item_key}_replacement_clear",
                name=f"{_friendly_item_name(item)} replacement clear",
                icon=CONSUMABLE_CLEAR_ICONS.get(item, "mdi:package-variant-closed"),
                replace_items=(code,),
            )
        )
    return tuple(descriptions)


CONSUMABLE_INFO_RESET_DESCRIPTIONS = _maintenance_reset_descriptions()


@callback
def _async_migrate_consumable_info_reset_unique_ids(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
) -> None:
    """Migrate reset buttons created by earlier fork revisions."""
    device_id = entry.data["device_id"]
    registry = er.async_get(hass)
    for description in CONSUMABLE_INFO_RESET_DESCRIPTIONS:
        old_unique_id = f"{device_id}_{description.suggested_key}"
        new_unique_id = f"{device_id}_{description.key}"
        if old_unique_id == new_unique_id:
            continue
        old_entity_id = registry.async_get_entity_id(
            "button", DOMAIN, old_unique_id
        )
        if old_entity_id is None:
            continue
        old_entry = registry.async_get(old_entity_id)
        if (
            old_entry is None
            or old_entry.platform != DOMAIN
            or old_entry.config_entry_id != entry.entry_id
        ):
            continue
        new_entity_id = registry.async_get_entity_id(
            "button", DOMAIN, new_unique_id
        )
        if new_entity_id is not None:
            new_entry = registry.async_get(new_entity_id)
            if (
                new_entry is None
                or new_entry.platform != DOMAIN
                or new_entry.config_entry_id != entry.entry_id
            ):
                continue
            registry.async_remove(new_entity_id)
        registry.async_update_entity(old_entity_id, new_unique_id=new_unique_id)


def _active_consumable_info_reset_descriptions(
    state,
) -> tuple[NarwalConsumableInfoResetDescription, ...]:
    """Return reset button descriptions for currently reported alert items."""
    maintain_items = set(getattr(state, "maintain_items", ()))
    replace_items = set(getattr(state, "replace_items", ()))
    descriptions = [
        description
        for description in CONSUMABLE_INFO_RESET_DESCRIPTIONS
        if (
            maintain_items.intersection(description.maintain_items)
            or replace_items.intersection(description.replace_items)
        )
    ]

    known_maintain_items = set(CONSUMABLE_MAINTAIN_ITEMS)
    known_replace_items = set(CONSUMABLE_REPLACE_ITEMS)
    for code in sorted(maintain_items - known_maintain_items):
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"maintenance_{code}_clear",
                suggested_key=f"maintenance_code_{code}_clear",
                name=f"Maintenance code {code} clear",
                icon="mdi:wrench-clock",
                maintain_items=(code,),
            )
        )
    for code in sorted(replace_items - known_replace_items):
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"replacement_{code}_clear",
                suggested_key=f"replacement_code_{code}_clear",
                name=f"Replacement code {code} clear",
                icon="mdi:package-variant-closed",
                replace_items=(code,),
            )
        )

    return tuple(descriptions)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal consumable button entities."""
    _async_migrate_consumable_info_reset_unique_ids(hass, entry)
    coordinator = entry.runtime_data
    known_consumable_info_resets: set[str] = set()

    @callback
    def async_add_consumable_info_reset_buttons() -> None:
        state = coordinator.data or coordinator.client.state
        if state is None:
            return
        new_descriptions = sorted(
            (
                description
                for description in _active_consumable_info_reset_descriptions(state)
                if description.key not in known_consumable_info_resets
            ),
            key=lambda description: description.name.lower(),
        )
        if not new_descriptions:
            return
        known_consumable_info_resets.update(
            description.key for description in new_descriptions
        )
        async_add_entities(
            NarwalConsumableInfoResetButton(coordinator, description)
            for description in new_descriptions
        )

    async_add_consumable_info_reset_buttons()
    entry.async_on_unload(
        coordinator.async_add_listener(async_add_consumable_info_reset_buttons)
    )

    known_consumables: set[str] = set()

    @callback
    def async_add_consumable_buttons() -> None:
        new_consumables = sorted(
            (
                consumable
                for code, consumable in coordinator.cloud_consumables.items()
                if code not in known_consumables and consumable.reset_supported
            ),
            key=lambda item: item.name.lower(),
        )
        if not new_consumables:
            return
        known_consumables.update(item.code for item in new_consumables)
        async_add_entities(
            NarwalConsumableResetButton(coordinator, consumable)
            for consumable in new_consumables
        )

    async_add_consumable_buttons()
    entry.async_on_unload(coordinator.async_add_listener(async_add_consumable_buttons))


class NarwalConsumableResetButton(NarwalEntity, ButtonEntity):
    """Button entity for resetting a cloud consumable counter."""

    _attr_icon = "mdi:restore"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        consumable: NarwalCloudConsumable,
    ) -> None:
        """Initialize the consumable reset button."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._consumable_code = consumable.code
        self._attr_unique_id = (
            f"{device_id}_consumable_{slugify(consumable.code)}_reset"
        )
        self._attr_suggested_object_id = (
            f"{slugify(coordinator.config_entry.title)}_{slugify(consumable.name)}_reset"
        )
        self._attr_name = f"{consumable.name} reset"
        if is_dock_consumable_identity(consumable.code, consumable.name):
            self._use_dock_device_info()

    @property
    def available(self) -> bool:
        """Return True when this consumable can be reset."""
        if self.coordinator.cloud_consumables_error is not None:
            return False
        consumable = self.coordinator.cloud_consumables.get(self._consumable_code)
        return (
            consumable is not None
            and consumable.reset_supported
            and consumable.usage_duration > 0
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | float | bool] | None:
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
            "overdue": consumable.is_overdue,
        }

    async def async_press(self) -> None:
        """Reset the Narwal cloud consumable counter."""
        if not self.available:
            raise HomeAssistantError("Narwal consumable reset is not available")
        consumable = self.coordinator.cloud_consumables.get(self._consumable_code)
        if consumable is None:
            raise HomeAssistantError("Narwal consumable is not available")
        if not consumable.reset_supported:
            raise HomeAssistantError(
                f"Narwal {consumable.name} reset is not supported"
            )
        try:
            await self.coordinator.async_reset_cloud_consumable(self._consumable_code)
        except NarwalCloudError as err:
            raise HomeAssistantError(str(err)) from err


class NarwalConsumableInfoResetButton(NarwalEntity, ButtonEntity):
    """Button entity for clearing robot-reported consumable alert items."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalConsumableInfoResetDescription,
    ) -> None:
        """Initialize the consumable-info reset button."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        title = slugify(coordinator.config_entry.title)
        self.description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_suggested_object_id = f"{title}_{description.suggested_key}"
        self._attr_name = description.name
        self._attr_icon = description.icon
        if is_dock_consumable_name(description.name):
            self._use_dock_device_info()

    @property
    def available(self) -> bool:
        """Return True when this alert clear button has an active item to clear."""
        if not super().available:
            return False
        state = self.coordinator.data or self.coordinator.client.state
        if state is None:
            return False
        return bool(
            set(self.description.maintain_items).intersection(state.maintain_items)
            or set(self.description.replace_items).intersection(state.replace_items)
        )

    @property
    def extra_state_attributes(self) -> dict[str, list[int] | list[str]] | None:
        """Return consumable alert clear context."""
        items: list[str] = []
        for code in self.description.maintain_items:
            items.append(CONSUMABLE_MAINTAIN_ITEMS.get(code, str(code)))
        for code in self.description.replace_items:
            items.append(CONSUMABLE_REPLACE_ITEMS.get(code, str(code)))
        return {
            "maintain_items": list(self.description.maintain_items),
            "replace_items": list(self.description.replace_items),
            "items": items,
        }

    async def async_press(self) -> None:
        """Clear this consumable alert item from the robot alert list."""
        if not self.available:
            raise HomeAssistantError("Narwal consumable alert is not active")

        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)

        response = await client.reset_consumable_info(
            maintain_items=self.description.maintain_items,
            replace_items=self.description.replace_items,
        )
        if not response.accepted:
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal consumable alert clear failed: {result_name}"
            )

        if (
            set(self.description.maintain_items).intersection(client.state.maintain_items)
            or set(self.description.replace_items).intersection(client.state.replace_items)
        ):
            raise HomeAssistantError("Narwal consumable alert did not clear")

        self.coordinator.async_set_updated_data(client.state)
