"""Clean-parameter select entities for Narwal vacuum.

These hold pending values applied at the next room clean. Water additionally writes
live via clean/set_mop_humidity while cleaning.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .const import (
    FAN_SPEED_LIST,
    FAN_SPEED_MAP,
    fan_speed_list_for,
    MOP_STRENGTH_MAP,
    WATER_MAP,
    WORK_MODE_MAP,
)
from .coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    clean_setting_applies_to_mode,
    is_live_clean_setting_available,
)
from .entity import NarwalEntity
from .narwal_client import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)


def _raise_if_command_failed(response, action: str) -> None:
    """Raise a Home Assistant service error for rejected robot commands."""
    if response.success:
        return
    try:
        result_name = CommandResult(response.result_code).name
    except ValueError:
        result_name = f"UNKNOWN({response.result_code})"
    raise HomeAssistantError(f"Narwal {action} failed: {result_name}")


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
LEGACY_MODE_LABELS: dict[WorkMode, str] = {
    value: label for label, value in LEGACY_MODE_MAP.items()
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
LEGACY_ROUTE_MAP: dict[str, CleaningRoute] = {
    "Standard": CleaningRoute.STANDARD,
    "Meticulous": CleaningRoute.METICULOUS,
}
LEGACY_MOP_MODES = {"Mop", "Vacuum then mop", "Vacuum and mop"}
LEGACY_VACUUM_MODES = {"Vacuum", "Vacuum then mop", "Vacuum and mop"}
LEGACY_START_ONLY_SETTINGS = {"mode", "passes", "route", "scrub"}
START_ONLY_CLEAN_SETTING_ATTRS = {"work_mode", "mop_strength"}


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


@dataclass(frozen=True, kw_only=True)
class RoomNarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a per-room Narwal clean profile select."""

    setting_key: str
    attr: str
    default_option: str
    icon: str


ROOM_SELECT_DESCRIPTIONS: tuple[RoomNarwalSelectEntityDescription, ...] = (
    RoomNarwalSelectEntityDescription(
        key="room_mode",
        setting_key="mode",
        attr="work_mode",
        name="mode",
        default_option="Vacuum and mop",
        icon="mdi:robot-vacuum",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_suction",
        setting_key="suction",
        attr="fan",
        name="suction",
        default_option="Super",
        icon="mdi:fan",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_water",
        setting_key="water",
        attr="water",
        name="water",
        default_option="Wet",
        icon="mdi:water",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_scrub",
        setting_key="scrub",
        attr="mop_strength",
        name="scrub",
        default_option="High",
        icon="mdi:brush",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_route",
        setting_key="route",
        attr="route",
        name="route",
        default_option="Meticulous",
        icon="mdi:routes",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_passes",
        setting_key="passes",
        attr="passes",
        name="passes",
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
    known_room_settings: dict[tuple[str | None, int, str], RoomNarwalSettingSelect] = {}

    @callback
    def async_add_room_setting_entities() -> None:
        map_data = coordinator.client.state.map_data
        if map_data is None:
            return
        map_id = coordinator.room_settings_map_id(map_data)
        entities: list[RoomNarwalSettingSelect] = []
        for room in sorted(map_data.rooms, key=lambda item: item.display_name.lower()):
            if room.room_id <= 0:
                continue
            for description in ROOM_SELECT_DESCRIPTIONS:
                key = (map_id, room.room_id, description.key)
                if key in known_room_settings:
                    known_room_settings[key].async_update_room_name(room.display_name)
                    continue
                entity = RoomNarwalSettingSelect(
                    coordinator,
                    room.room_id,
                    room.display_name,
                    description,
                    map_id=map_id,
                )
                known_room_settings[key] = entity
                entities.append(entity)
        if entities:
            async_add_entities(entities)

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
    async_add_room_setting_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_room_setting_entities))


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
        """Return True when this clean parameter can be changed now."""
        if not super().available:
            return False
        if not clean_setting_applies_to_mode(
            self.entity_description.attr,
            self.coordinator.clean_settings.work_mode,
        ):
            return False
        if self.entity_description.attr in START_ONLY_CLEAN_SETTING_ATTRS:
            return can_edit_pending_clean_settings(self.coordinator.data)
        return (
            can_edit_pending_clean_settings(self.coordinator.data)
            or is_live_clean_setting_available(self.coordinator.data)
        )

    @property
    def current_option(self) -> str | None:
        """Return the stored option label."""
        value = getattr(self.coordinator.clean_settings, self.entity_description.attr)
        return self._labels.get(int(value))

    async def async_select_option(self, option: str) -> None:
        """Store the selection and, for live controls, apply it if cleaning."""
        if not super().available:
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        state = self.coordinator.data
        setup_available = can_edit_pending_clean_settings(state)
        live_available = is_live_clean_setting_available(state)
        if not clean_setting_applies_to_mode(
            self.entity_description.attr,
            self.coordinator.clean_settings.work_mode,
        ):
            raise HomeAssistantError(
                "This Narwal setting is not available for the selected mode"
            )
        if (
            self.entity_description.attr in START_ONLY_CLEAN_SETTING_ATTRS
            and not setup_available
        ):
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        if (
            self.entity_description.attr not in START_ONLY_CLEAN_SETTING_ATTRS
            and not setup_available
            and not live_available
        ):
            raise HomeAssistantError("This Narwal setting cannot be changed right now")

        value = self.entity_description.mapping[option]
        if (
            self.entity_description.live_setter
            and state is not None
            and live_available
        ):
            response = await getattr(
                self.coordinator.client, self.entity_description.live_setter
            )(
                value
            )
            _raise_if_command_failed(response, f"set {self.entity_description.name}")
        setattr(self.coordinator.clean_settings, self.entity_description.attr, value)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class RoomNarwalSettingSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Per-room clean profile select backed by coordinator room settings."""

    entity_description: RoomNarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        room_id: int,
        room_name: str,
        description: RoomNarwalSelectEntityDescription,
        *,
        map_id: str | None = None,
    ) -> None:
        """Initialize the per-room select."""
        super().__init__(coordinator)
        self.entity_description = description
        self._map_id = map_id
        self._room_id = room_id
        self._room_name = room_name
        device_id = coordinator.config_entry.data["device_id"]
        map_prefix = f"map_{slugify(map_id)}_" if map_id is not None else ""
        self._attr_unique_id = (
            f"{device_id}_{map_prefix}room_{room_id}_{description.setting_key}"
        )
        self._attr_name = f"{room_name} {description.name}"
        self._attr_icon = description.icon
        self._attr_options = self._options_for_description(description)
        self._attr_entity_category = EntityCategory.CONFIG

    @callback
    def async_update_room_name(self, room_name: str) -> None:
        """Update display metadata when the map renames this room."""
        if room_name == self._room_name:
            return
        self._room_name = room_name
        self._attr_name = f"{room_name} {self.entity_description.name}"
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    def _options_for_description(
        self,
        description: RoomNarwalSelectEntityDescription,
    ) -> list[str]:
        """Return selectable options for this room setting."""
        if description.setting_key == "suction":
            return ["AI", *fan_speed_list_for(self.coordinator.config_entry.data)]
        if description.setting_key == "mode":
            return list(LEGACY_MODE_OPTIONS)
        if description.setting_key == "water":
            return list(LEGACY_WATER_OPTIONS)
        if description.setting_key == "scrub":
            return list(LEGACY_SCRUB_OPTIONS)
        if description.setting_key == "route":
            return list(LEGACY_ROUTE_OPTIONS)
        if description.setting_key == "passes":
            return list(LEGACY_PASSES_OPTIONS)
        return []

    async def async_added_to_hass(self) -> None:
        """Restore the room profile option."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        option = self._restored_option(last)
        if option is not None:
            self._apply_option(option)

    @property
    def available(self) -> bool:
        """Return True when this room profile can be changed now."""
        if (
            not super().available
            or not can_edit_pending_clean_settings(self.coordinator.data)
            or not self._room_exists
        ):
            return False
        key = self.entity_description.setting_key
        mode = self._selected_mode
        if key == "water" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "suction" and mode not in LEGACY_VACUUM_MODES:
            return False
        return True

    @property
    def options(self) -> list[str]:
        """Return the selectable room profile options."""
        return list(self._attr_options or [])

    @property
    def current_option(self) -> str | None:
        """Return the currently selected profile option."""
        settings = self.coordinator.effective_room_clean_settings_for(
            self._room_id,
            map_id=self._map_id,
        )
        return self._label_for_value(getattr(settings, self.entity_description.attr))

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool]:
        """Return room metadata for dashboards and automations."""
        return {
            "room_id": self._room_id,
            "room_name": self._room_name,
            "setting": self.entity_description.setting_key,
            "map_id": self._map_id or "",
            "customized": self._is_customized,
        }

    @property
    def _is_customized(self) -> bool:
        """Return True when this room field was explicitly customized."""
        customized = getattr(self.coordinator, "room_clean_settings_customized", {})
        key = (self._map_id, self._room_id)
        return self.entity_description.attr in customized.get(key, set())

    @property
    def _room_exists(self) -> bool:
        """Return True when the room still exists in the current map."""
        state = self.coordinator.data
        map_data = getattr(state, "map_data", None) if state is not None else None
        if self.coordinator.room_settings_map_id(map_data) != self._map_id:
            return False
        rooms = getattr(map_data, "rooms", None)
        if not isinstance(rooms, (list, tuple)):
            return True
        return any(room.room_id == self._room_id for room in rooms)

    @property
    def _selected_mode(self) -> str:
        """Return the selected room clean mode."""
        settings = self.coordinator.effective_room_clean_settings_for(
            self._room_id,
            map_id=self._map_id,
        )
        return LEGACY_MODE_LABELS.get(settings.work_mode) or "Vacuum and mop"

    def _label_for_value(self, value) -> str | None:
        """Return the UI option label for a profile value."""
        key = self.entity_description.setting_key
        if key == "mode":
            return LEGACY_MODE_LABELS.get(value)
        if key == "suction":
            return LEGACY_SUCTION_LABELS.get(value)
        if key == "water":
            labels = {value: label for label, value in LEGACY_WATER_MAP.items()}
            return labels.get(value)
        if key == "scrub":
            labels = {value: label for label, value in LEGACY_SCRUB_MAP.items()}
            return labels.get(value)
        if key == "route":
            labels = {value: label for label, value in LEGACY_ROUTE_MAP.items()}
            return labels.get(value)
        if key == "passes":
            return str(value)
        return None

    def _normalise_option(self, option: str) -> str | None:
        """Return a current option, accepting old hidden suction aliases."""
        if option in self.options:
            return option
        if self.entity_description.setting_key != "suction":
            return None
        label = LEGACY_SUCTION_LABELS.get(LEGACY_SUCTION_MAP.get(option))
        return label if label in self.options else None

    def _restored_option(self, state: State | None) -> str | None:
        """Return a persisted room profile option, if one exists."""
        if state is not None and self._restore_state_is_customized(state):
            option = self._normalise_option(state.state)
            if option is not None:
                return option
        return None

    @staticmethod
    def _restore_state_is_customized(state: State) -> bool:
        """Return True when restored state represents an explicit room override."""
        attributes = getattr(state, "attributes", None)
        return isinstance(attributes, dict) and attributes.get("customized") is True

    def _apply_option(self, option: str) -> None:
        """Store a room profile option."""
        key = self.entity_description.setting_key
        if key == "mode":
            value = LEGACY_MODE_MAP[option]
        elif key == "suction":
            value = LEGACY_SUCTION_MAP[option]
        elif key == "water":
            value = LEGACY_WATER_MAP[option]
        elif key == "scrub":
            value = LEGACY_SCRUB_MAP[option]
        elif key == "route":
            value = LEGACY_ROUTE_MAP[option]
        elif key == "passes":
            value = int(option)
        else:
            raise HomeAssistantError(f"Unsupported Narwal room option: {option}")
        self.coordinator.set_room_clean_setting(
            self._room_id,
            self.entity_description.attr,
            value,
            map_id=self._map_id,
        )

    async def async_select_option(self, option: str) -> None:
        """Apply a room profile option."""
        if not super().available:
            raise HomeAssistantError("Narwal room profiles cannot be changed right now")
        requested_option = option
        option = self._normalise_option(option) or ""
        if not option:
            raise HomeAssistantError(f"Unsupported Narwal room option: {requested_option}")
        if not self._room_exists:
            raise HomeAssistantError("Narwal room is not available")
        if not can_edit_pending_clean_settings(self.coordinator.data):
            raise HomeAssistantError("Narwal room profiles cannot be changed right now")

        key = self.entity_description.setting_key
        mode = self._selected_mode
        if key == "water" and mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Water level is not available in vacuum-only mode")
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Scrub level is not available in vacuum-only mode")
        if key == "suction" and mode not in LEGACY_VACUUM_MODES:
            raise HomeAssistantError("Suction is not available in mop-only mode")

        self._apply_option(option)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


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
        self._attr_entity_category = EntityCategory.CONFIG
        if description.setting_key == "suction":
            self._attr_options = [
                "AI",
                *fan_speed_list_for(coordinator.config_entry.data),
            ]
        else:
            self._attr_options = list(description.setting_options)
        self._option = description.default_option

    async def async_added_to_hass(self) -> None:
        """Restore legacy-only settings without competing with primary entities."""
        await super().async_added_to_hass()
        option = self._restored_option(await self.async_get_last_state())
        if option is not None and self.entity_description.setting_key == "route":
            self._apply_option(option)
            return
        self._option = self._option_from_settings()

    @property
    def available(self) -> bool:
        """Return True when this legacy setting can be changed now."""
        return super().available and self._setting_available()

    @property
    def options(self) -> list[str]:
        """Return the static option list for Home Assistant capabilities."""
        return list(self._attr_options or [])

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self._option_from_settings()

    @property
    def _selected_mode(self) -> str:
        """Return the selected legacy clean mode."""
        return (
            LEGACY_MODE_LABELS.get(self.coordinator.clean_settings.work_mode)
            or "Vacuum and mop"
        )

    @property
    def _is_cleaning_or_paused(self) -> bool:
        """Return True while the robot is in an active clean session."""
        return is_live_clean_setting_available(self.coordinator.data)

    def _setting_available(self) -> bool:
        """Return whether this setting is currently meaningful and actionable."""
        key = self.entity_description.setting_key
        if key == "water" and self._selected_mode not in LEGACY_MOP_MODES:
            return False
        if key == "scrub" and self._selected_mode not in LEGACY_MOP_MODES:
            return False
        if key == "suction" and self._selected_mode not in LEGACY_VACUUM_MODES:
            return False
        return key not in LEGACY_START_ONLY_SETTINGS or not self._is_cleaning_or_paused

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
        elif key == "route":
            settings.route = LEGACY_ROUTE_MAP[option]
        elif key == "passes":
            settings.passes = int(option)

    def _option_from_settings(self) -> str:
        """Return this legacy option from the shared clean settings."""
        key = self.entity_description.setting_key
        settings = self.coordinator.clean_settings
        if key == "mode":
            option = LEGACY_MODE_LABELS.get(settings.work_mode)
        elif key == "suction":
            option = LEGACY_SUCTION_LABELS.get(settings.fan)
        elif key == "water":
            option = {value: label for label, value in LEGACY_WATER_MAP.items()}.get(
                settings.water
            )
        elif key == "scrub":
            option = {value: label for label, value in LEGACY_SCRUB_MAP.items()}.get(
                settings.mop_strength
            )
        elif key == "route":
            option = {value: label for label, value in LEGACY_ROUTE_MAP.items()}.get(
                settings.route
            )
        elif key == "passes":
            option = str(settings.passes)
        else:
            option = None
        return self._normalise_option(option or "") or self.entity_description.default_option

    def _normalise_option(self, option: str) -> str | None:
        """Return a current option, accepting old hidden suction aliases."""
        if option in self.options:
            return option
        if self.entity_description.setting_key != "suction":
            return None
        label = LEGACY_SUCTION_LABELS.get(LEGACY_SUCTION_MAP.get(option))
        return label if label in self.options else None

    def _restored_option(self, state: State | None) -> str | None:
        """Return a current option, accepting old hidden aliases from restore state."""
        if state is None:
            return None
        return self._normalise_option(state.state)

    async def async_select_option(self, option: str) -> None:
        """Apply a legacy setting option."""
        if not super().available:
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
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
        self.coordinator.async_update_listeners()
