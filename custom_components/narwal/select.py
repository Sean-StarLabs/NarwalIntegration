"""Select entities for Narwal vacuum controls."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import (
    FAN_SPEED_LIST,
    FAN_SPEED_MAP,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResult, MopHumidity, WorkingStatus

MODE_OPTIONS = ("Vacuum", "Mop", "Vacuum then mop", "Vacuum and mop")
DEFAULT_MODE = "Vacuum and mop"
SUCTION_OPTIONS = ("AI", *FAN_SPEED_LIST)
WATER_OPTIONS = ("Dry", "Normal", "Wet")
SCRUB_OPTIONS = ("Normal", "High")
ROUTE_OPTIONS = ("Standard", "Meticulous")
PASSES_OPTIONS = ("1", "2", "3")

ACTIVE_CLEANING_STATUSES = (
    WorkingStatus.CLEANING,
    WorkingStatus.CLEANING_V2,
    WorkingStatus.CLEANING_ALT,
    WorkingStatus.CLEANING_FLOW2,
)

MOP_MODES = {"Mop", "Vacuum then mop", "Vacuum and mop"}
VACUUM_MODES = {"Vacuum", "Vacuum then mop", "Vacuum and mop"}
START_ONLY_SETTINGS = {"mode", "passes", "route", "scrub"}

RUNTIME_SUCTION_KEY = "runtime_suction"
RUNTIME_WATER_KEY = "runtime_water"

SETTING_KEYS = {
    "mode",
    "suction",
    "water",
    "scrub",
    "route",
    "passes",
}

WATER_OPTION_VALUES: dict[str, MopHumidity] = {
    "Dry": MopHumidity.DRY,
    "Normal": MopHumidity.NORMAL,
    "Wet": MopHumidity.WET,
}


@dataclass(frozen=True, kw_only=True)
class NarwalSettingSelectEntityDescription(SelectEntityDescription):
    """Describes a Narwal setting select."""

    setting_key: str
    setting_options: tuple[str, ...]
    default_option: str
    icon: str


SETTING_SELECT_DESCRIPTIONS: tuple[NarwalSettingSelectEntityDescription, ...] = (
    NarwalSettingSelectEntityDescription(
        key="mode",
        setting_key="mode",
        translation_key="mode",
        setting_options=MODE_OPTIONS,
        default_option=DEFAULT_MODE,
        icon="mdi:robot-vacuum",
    ),
    NarwalSettingSelectEntityDescription(
        key=RUNTIME_SUCTION_KEY,
        setting_key="suction",
        translation_key="suction",
        setting_options=SUCTION_OPTIONS,
        default_option="AI",
        icon="mdi:fan",
    ),
    NarwalSettingSelectEntityDescription(
        key=RUNTIME_WATER_KEY,
        setting_key="water",
        translation_key="water",
        setting_options=WATER_OPTIONS,
        default_option="Wet",
        icon="mdi:water",
    ),
    NarwalSettingSelectEntityDescription(
        key="scrub",
        setting_key="scrub",
        translation_key="scrub",
        setting_options=SCRUB_OPTIONS,
        default_option="High",
        icon="mdi:brush",
    ),
    NarwalSettingSelectEntityDescription(
        key="route",
        setting_key="route",
        translation_key="route",
        setting_options=ROUTE_OPTIONS,
        default_option="Meticulous",
        icon="mdi:routes",
    ),
    NarwalSettingSelectEntityDescription(
        key="passes",
        setting_key="passes",
        translation_key="passes",
        setting_options=PASSES_OPTIONS,
        default_option="2",
        icon="mdi:counter",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal select entities."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        NarwalSettingSelect(coordinator, description)
        for description in SETTING_SELECT_DESCRIPTIONS
    ]
    async_add_entities(entities)


class NarwalSettingSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Select entity for Narwal start settings and supported runtime controls."""

    entity_description: NarwalSettingSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSettingSelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon
        self._attr_options = description.setting_options

    @property
    def _settings(self) -> dict[str, str]:
        """Return coordinator-backed selected options."""
        return self.coordinator.select_options

    async def async_added_to_hass(self) -> None:
        """Restore the last selected option."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self.options:
            self._settings[self.entity_description.setting_key] = last_state.state
            return
        self._settings.setdefault(
            self.entity_description.setting_key,
            self._default_option,
        )

    @property
    def _default_option(self) -> str:
        """Return the default option."""
        return self.entity_description.default_option

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self._settings.get(
            self.entity_description.setting_key,
            self._default_option,
        )

    @property
    def available(self) -> bool:
        """Return True when the setting can be changed."""
        if not super().available:
            return False

        mode = self._selected_mode
        key = self.entity_description.setting_key
        if key == "water" and mode not in MOP_MODES:
            return False
        if key == "scrub" and mode not in MOP_MODES:
            return False
        if key == "suction" and mode not in VACUUM_MODES:
            return False
        if key in START_ONLY_SETTINGS and self._is_cleaning_or_paused:
            return False
        return True

    @property
    def _selected_mode(self) -> str:
        """Return the selected clean mode."""
        return self._settings.get("mode", DEFAULT_MODE)

    @property
    def _is_cleaning_or_paused(self) -> bool:
        """Return True while the robot is in an active clean session."""
        state = self.coordinator.data
        if state is None:
            return False
        return (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
        ) and not state.is_docked and not state.is_returning

    async def async_select_option(self, option: str) -> None:
        """Apply a setting option."""
        if option not in self.options:
            raise HomeAssistantError(f"Unsupported Narwal option: {option}")

        key = self.entity_description.setting_key
        if key == "water" and self._selected_mode not in MOP_MODES:
            raise HomeAssistantError("Water level is not available in vacuum-only mode")
        if key == "scrub" and self._selected_mode not in MOP_MODES:
            raise HomeAssistantError("Scrub level is not available in vacuum-only mode")
        if key == "suction" and self._selected_mode not in VACUUM_MODES:
            raise HomeAssistantError("Suction is not available in mop-only mode")
        if key in START_ONLY_SETTINGS and self._is_cleaning_or_paused:
            raise HomeAssistantError("This Narwal setting cannot be changed mid-clean")
        if key == "suction" and option == "AI" and self._is_cleaning_or_paused:
            raise HomeAssistantError("AI suction cannot be selected mid-clean")

        response = None
        if self._is_cleaning_or_paused:
            if key == "suction":
                response = await self.coordinator.client.set_fan_speed(
                    FAN_SPEED_MAP[option]
                )
            elif key == "water":
                response = await self.coordinator.client.set_mop_humidity(
                    WATER_OPTION_VALUES[option]
                )

        if (
            response is not None
            and response.result_code not in (0, CommandResult.SUCCESS)
        ):
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal setting command failed: {result_name}"
            )

        self._settings[key] = option
        self.async_write_ha_state()
