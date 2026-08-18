"""Clean-parameter select entities for Narwal vacuum.

These hold pending values applied at the next room clean. Water additionally writes
live via clean/set_mop_humidity while cleaning.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import (
    FAN_SPEED_LIST,
    FAN_SPEED_MAP,
    MOP_STRENGTH_MAP,
    WATER_MAP,
    WORK_MODE_MAP,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import (
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)
from .narwal_client.const import ACTIVE_CLEANING_STATUSES


@dataclass(frozen=True, kw_only=True)
class NarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a Narwal clean-param select."""

    attr: str  # CleanSettings field this select reads/writes
    mapping: dict[str, int]  # option label -> robot enum value
    live_setter: str | None = None  # NarwalClient coroutine applied live while cleaning


SELECT_DESCRIPTIONS: tuple[NarwalSelectEntityDescription, ...] = (
    NarwalSelectEntityDescription(
        key="work_mode",
        translation_key="work_mode",
        entity_category=EntityCategory.CONFIG,
        attr="work_mode",
        mapping=WORK_MODE_MAP,
        options=list(WORK_MODE_MAP),
    ),
    NarwalSelectEntityDescription(
        key="water",
        translation_key="water",
        entity_category=EntityCategory.CONFIG,
        attr="water",
        mapping=WATER_MAP,
        live_setter="set_mop_humidity",
        options=list(WATER_MAP),
    ),
    NarwalSelectEntityDescription(
        key="mop_strength",
        translation_key="mop_strength",
        entity_category=EntityCategory.CONFIG,
        attr="mop_strength",
        mapping=MOP_STRENGTH_MAP,
        options=list(MOP_STRENGTH_MAP),
    ),
)

LEGACY_MODE_OPTIONS = ("Vacuum", "Mop", "Vacuum then mop", "Vacuum and mop")
LEGACY_SUCTION_OPTIONS = ("AI", *FAN_SPEED_LIST)
LEGACY_WATER_OPTIONS = ("Dry", "Normal", "Wet")
LEGACY_SCRUB_OPTIONS = ("Normal", "High")
LEGACY_ROUTE_OPTIONS = ("Standard", "Meticulous")
LEGACY_PASSES_OPTIONS = ("1", "2", "3")

LEGACY_MODE_MAP: dict[str, WorkMode] = {
    "Vacuum": WorkMode.VACUUM,
    "Mop": WorkMode.MOP,
    "Vacuum then mop": WorkMode.VACUUM_THEN_MOP,
    "Vacuum and mop": WorkMode.VACUUM_AND_MOP,
}
LEGACY_SUCTION_MAP: dict[str, FanLevel] = {
    "AI": FanLevel.UNSPECIFIED,
    "Super powerful": FanLevel.DEEP,
    "Ultra powerful": FanLevel.SUPER,
    **{option: FAN_SPEED_MAP[option] for option in FAN_SPEED_LIST},
}
LEGACY_SUCTION_LABELS: dict[FanLevel, str] = {
    FanLevel.UNSPECIFIED: "AI",
    **{FAN_SPEED_MAP[option]: option for option in FAN_SPEED_LIST},
}
LEGACY_WATER_MAP: dict[str, MopHumidity] = {
    "Dry": MopHumidity.DRY,
    "Normal": MopHumidity.NORMAL,
    "Wet": MopHumidity.WET,
}
LEGACY_SCRUB_MAP: dict[str, MopStrengthLevel] = {
    "Normal": MopStrengthLevel.NORMAL,
    "High": MopStrengthLevel.HIGH,
}
LEGACY_MOP_MODES = {"Mop", "Vacuum then mop", "Vacuum and mop"}
LEGACY_VACUUM_MODES = {"Vacuum", "Vacuum then mop", "Vacuum and mop"}
LEGACY_START_ONLY_SETTINGS = {"mode", "passes", "route", "scrub"}


@dataclass(frozen=True, kw_only=True)
class LegacyNarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a backwards-compatible Narwal setting select."""

    setting_key: str
    setting_options: tuple[str, ...]
    default_option: str
    icon: str


LEGACY_SELECT_DESCRIPTIONS: tuple[LegacyNarwalSelectEntityDescription, ...] = (
    LegacyNarwalSelectEntityDescription(
        key="mode",
        setting_key="mode",
        name="Mode",
        setting_options=LEGACY_MODE_OPTIONS,
        default_option="Vacuum and mop",
        icon="mdi:robot-vacuum",
    ),
    LegacyNarwalSelectEntityDescription(
        key="runtime_suction",
        setting_key="suction",
        name="Suction",
        setting_options=LEGACY_SUCTION_OPTIONS,
        default_option="Super",
        icon="mdi:fan",
    ),
    LegacyNarwalSelectEntityDescription(
        key="runtime_water",
        setting_key="water",
        name="Water",
        setting_options=LEGACY_WATER_OPTIONS,
        default_option="Wet",
        icon="mdi:water",
    ),
    LegacyNarwalSelectEntityDescription(
        key="scrub",
        setting_key="scrub",
        name="Scrub",
        setting_options=LEGACY_SCRUB_OPTIONS,
        default_option="High",
        icon="mdi:brush",
    ),
    LegacyNarwalSelectEntityDescription(
        key="route",
        setting_key="route",
        name="Route",
        setting_options=LEGACY_ROUTE_OPTIONS,
        default_option="Meticulous",
        icon="mdi:routes",
    ),
    LegacyNarwalSelectEntityDescription(
        key="passes",
        setting_key="passes",
        name="Passes",
        setting_options=LEGACY_PASSES_OPTIONS,
        default_option="2",
        icon="mdi:counter",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal clean-param select entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(
                NarwalSelect(coordinator, description)
                for description in SELECT_DESCRIPTIONS
            ),
            *(
                LegacyNarwalSettingSelect(coordinator, description)
                for description in LEGACY_SELECT_DESCRIPTIONS
            ),
        ]
    )


class NarwalSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Clean-parameter select backed by coordinator.clean_settings."""

    entity_description: NarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._labels = {int(v): k for k, v in description.mapping.items()}

    async def async_added_to_hass(self) -> None:
        """Restore the last selection into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.entity_description.mapping:
            setattr(
                self.coordinator.clean_settings,
                self.entity_description.attr,
                self.entity_description.mapping[last.state],
            )

    @property
    def available(self) -> bool:
        """Editable even while the robot sleeps — these are pending settings."""
        return True

    @property
    def current_option(self) -> str | None:
        """Return the stored option label."""
        value = getattr(self.coordinator.clean_settings, self.entity_description.attr)
        return self._labels.get(int(value))

    async def async_select_option(self, option: str) -> None:
        """Store the selection and, for live controls, apply it if cleaning."""
        value = self.entity_description.mapping[option]
        setattr(self.coordinator.clean_settings, self.entity_description.attr, value)
        self.async_write_ha_state()
        state = self.coordinator.data
        if (
            self.entity_description.live_setter
            and state is not None
            and state.is_cleaning
        ):
            await getattr(self.coordinator.client, self.entity_description.live_setter)(
                value
            )


class LegacyNarwalSettingSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Backwards-compatible selects for existing dashboards and scripts."""

    entity_description: LegacyNarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: LegacyNarwalSelectEntityDescription,
    ) -> None:
        """Initialize the legacy select."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon
        self._attr_options = list(description.setting_options)
        self._option = description.default_option

    async def async_added_to_hass(self) -> None:
        """Restore the last valid selected option."""
        await super().async_added_to_hass()
        option = self._restored_option(await self.async_get_last_state())
        self._apply_option(option)

    @property
    def available(self) -> bool:
        """Return True when the setting can be changed."""
        if not super().available:
            return False
        mode = self._selected_mode
        key = self.entity_description.setting_key
        if key == "water" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "suction" and mode not in LEGACY_VACUUM_MODES:
            return False
        return not (key in LEGACY_START_ONLY_SETTINGS and self._is_cleaning_or_paused)

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self._option

    @property
    def _selected_mode(self) -> str:
        """Return the selected legacy clean mode."""
        if self.entity_description.setting_key == "mode":
            return self._option
        return getattr(self.coordinator, "_legacy_mode_option", "Vacuum and mop")

    @property
    def _is_cleaning_or_paused(self) -> bool:
        """Return True while the robot is in an active clean session."""
        state = self.coordinator.data
        if state is None:
            return False
        return (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_recent_active_working_status
        ) and not state.is_returning

    def _apply_option(self, option: str) -> None:
        """Store a legacy option and mirror it into clean settings."""
        self._option = option
        key = self.entity_description.setting_key
        settings = self.coordinator.clean_settings
        if key == "mode":
            settings.work_mode = LEGACY_MODE_MAP[option]
            setattr(self.coordinator, "_legacy_mode_option", option)
        elif key == "suction":
            settings.fan = LEGACY_SUCTION_MAP[option]
        elif key == "water":
            settings.water = LEGACY_WATER_MAP[option]
        elif key == "scrub":
            settings.mop_strength = LEGACY_SCRUB_MAP[option]
        elif key == "passes":
            settings.passes = int(option)

    def _normalise_option(self, option: str) -> str | None:
        """Return a current option, accepting old hidden suction aliases."""
        if option in self.entity_description.setting_options:
            return option
        if self.entity_description.setting_key != "suction":
            return None
        return LEGACY_SUCTION_LABELS.get(LEGACY_SUCTION_MAP.get(option))

    def _restored_option(self, state: State | None) -> str:
        """Return a current option, accepting old hidden aliases from restore state."""
        if state is None:
            return self.entity_description.default_option
        return self._normalise_option(state.state) or self.entity_description.default_option

    async def async_select_option(self, option: str) -> None:
        """Apply a legacy setting option."""
        requested_option = option
        option = self._normalise_option(option) or ""
        if not option:
            raise HomeAssistantError(f"Unsupported Narwal option: {requested_option}")

        key = self.entity_description.setting_key
        if key == "water" and self._selected_mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Water level is not available in vacuum-only mode")
        if key == "scrub" and self._selected_mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Scrub level is not available in vacuum-only mode")
        if key == "suction" and self._selected_mode not in LEGACY_VACUUM_MODES:
            raise HomeAssistantError("Suction is not available in mop-only mode")
        if key in LEGACY_START_ONLY_SETTINGS and self._is_cleaning_or_paused:
            raise HomeAssistantError("This Narwal setting cannot be changed mid-clean")
        if key == "suction" and option == "AI" and self._is_cleaning_or_paused:
            raise HomeAssistantError("AI suction cannot be selected mid-clean")

        response = None
        if self._is_cleaning_or_paused:
            if key == "suction":
                response = await self.coordinator.client.set_fan_speed(
                    LEGACY_SUCTION_MAP[option]
                )
            elif key == "water":
                response = await self.coordinator.client.set_mop_humidity(
                    LEGACY_WATER_MAP[option]
                )

        if response is not None and response.result_code not in (
            0,
            CommandResult.SUCCESS,
            CommandResult.APPLIED,
        ):
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal setting command failed: {result_name}"
            )

        self._apply_option(option)
        self.async_write_ha_state()
