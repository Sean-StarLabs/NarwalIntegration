"""Switch entities for Narwal map display options."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


@dataclass(frozen=True, kw_only=True)
class NarwalMapSwitchEntityDescription(SwitchEntityDescription):
    """Description for a Narwal map display switch."""

    default: bool


MAP_SWITCHES: tuple[NarwalMapSwitchEntityDescription, ...] = (
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_ROOM_LABELS,
        translation_key=CONF_SHOW_ROOM_LABELS,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_ROOM_LABELS],
    ),
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_FURNITURE,
        translation_key=CONF_SHOW_FURNITURE,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE],
    ),
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_FURNITURE_LABELS,
        translation_key=CONF_SHOW_FURNITURE_LABELS,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE_LABELS],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal switch entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalMapOptionSwitch(coordinator, description)
        for description in MAP_SWITCHES
    )


class NarwalMapOptionSwitch(NarwalEntity, SwitchEntity):
    """Persistent map display switch backed by config entry options."""

    entity_description: NarwalMapSwitchEntityDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalMapSwitchEntityDescription,
    ) -> None:
        """Initialize a map option switch."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map_{description.key}"

    @property
    def is_on(self) -> bool:
        """Return the current map option value."""
        return bool(
            self.coordinator.config_entry.options.get(
                self.entity_description.key,
                self.entity_description.default,
            )
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the map option on."""
        await self._async_set_option(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the map option off."""
        await self._async_set_option(False)

    async def _async_set_option(self, value: bool) -> None:
        """Persist the map option and notify camera listeners."""
        entry = self.coordinator.config_entry
        options = dict(entry.options)
        options[self.entity_description.key] = value
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.async_write_ha_state()
        if self.coordinator.data is not None:
            self.coordinator.async_update_listeners()
