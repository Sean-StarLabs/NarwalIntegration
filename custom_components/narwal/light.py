"""Light entities for Narwal dock lighting."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    DOCK_LIGHT_MODE_NAMES,
    DOCK_LIGHT_MODES,
    DOMAIN,
    is_dock_light_supported,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResult

_EFFECTS = tuple(mode for mode in DOCK_LIGHT_MODES if mode != "Off")
_DEFAULT_EFFECT = "Nightlight"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal dock light entities."""
    _cleanup_dock_light_registry(hass, entry)
    if not is_dock_light_supported(entry.data, entry.options):
        return
    async_add_entities([NarwalDockLight(entry.runtime_data)])


def _cleanup_dock_light_registry(hass: HomeAssistant, entry: NarwalConfigEntry) -> None:
    """Remove stale dock light entities from older development builds."""
    device_id = entry.data["device_id"]
    registry = er.async_get(hass)
    stale_unique_ids = {
        SELECT_DOMAIN: f"{device_id}_dock_light",
        LIGHT_DOMAIN: f"{device_id}_dock_light_light",
    }
    for registry_entry in list(registry.entities.values()):
        if registry_entry.platform != DOMAIN:
            continue
        for domain, unique_id in stale_unique_ids.items():
            if (
                registry_entry.entity_id.startswith(f"{domain}.")
                and registry_entry.unique_id == unique_id
            ):
                registry.async_remove(registry_entry.entity_id)


class NarwalDockLight(NarwalEntity, LightEntity):
    """Narwal dock ambient light with app effects."""

    _attr_translation_key = "dock_light"
    _attr_icon = "mdi:led-strip-variant"
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_effect_list = list(_EFFECTS)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the dock light."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_dock_light"
        self._last_effect = _DEFAULT_EFFECT

    @property
    def is_on(self) -> bool | None:
        """Return True when the dock light is on."""
        mode = self._current_mode
        if mode is None:
            return None
        return mode != "Off"

    @property
    def effect(self) -> str | None:
        """Return the active dock light effect."""
        mode = self._current_mode
        if mode is None or mode == "Off":
            return None
        self._last_effect = mode
        return mode

    @property
    def _current_mode(self) -> str | None:
        """Return the current dock light mode name."""
        state = self.coordinator.data
        if state is None:
            return None
        if state.dock_light_mode is None:
            return None
        return DOCK_LIGHT_MODE_NAMES.get(state.dock_light_mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the dock light on with the requested effect."""
        effect = kwargs.get(ATTR_EFFECT)
        if effect is None:
            effect = self.effect or self._last_effect
        if effect not in _EFFECTS:
            raise HomeAssistantError(f"Unsupported Narwal dock light effect: {effect}")
        await self._set_mode(effect)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the dock light off."""
        await self._set_mode("Off")

    async def _set_mode(self, mode: str) -> None:
        """Send the dock light command."""
        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)
        response = await client.set_ambient_light_mode(DOCK_LIGHT_MODES[mode])
        if response is None:
            raise HomeAssistantError("Narwal dock light command failed")
        if response.result_code not in (0, CommandResult.SUCCESS, CommandResult.APPLIED):
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(f"Narwal dock light command failed: {result_name}")

        if mode != "Off":
            self._last_effect = mode
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()
