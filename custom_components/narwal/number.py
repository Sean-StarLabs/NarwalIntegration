"""Number entity for the Narwal clean pass count.

Holds a pending value applied at the next room clean; the builder routes it to the right
CleanParam tag for the current mode (sweep->5, mop->6, sweep_then_mop->5+6, sync->7).
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import PASSES_MAX, PASSES_MIN
from .coordinator import NarwalCoordinator, is_active_clean_session
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal passes number entity."""
    async_add_entities([NarwalPassesNumber(entry.runtime_data)])


class NarwalPassesNumber(NarwalEntity, RestoreNumber):
    """Pending clean pass count, applied at the next room clean; restored across restarts."""

    _attr_translation_key = "passes"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = PASSES_MIN
    _attr_native_max_value = PASSES_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_id']}_passes"

    async def async_added_to_hass(self) -> None:
        """Restore the last pass count into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.coordinator.clean_settings.passes = int(last.native_value)

    @property
    def available(self) -> bool:
        """Return True when the pass count can be changed now."""
        return super().available and not is_active_clean_session(self.coordinator.data)

    @property
    def native_value(self) -> float:
        """Return the stored pass count."""
        return self.coordinator.clean_settings.passes

    async def async_set_native_value(self, value: float) -> None:
        """Store the pass count."""
        self.coordinator.clean_settings.passes = int(value)
        self.async_write_ha_state()
