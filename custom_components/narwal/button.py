"""Button entities for Narwal station maintenance actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .const import CONSUMABLE_MAINTAIN_ITEMS, CONSUMABLE_REPLACE_ITEMS
from .coordinator import (
    NarwalCoordinator,
    can_locate_robot,
    can_pause_cleaning,
    can_resume_cleaning,
    can_return_home,
    can_start_cleaning,
    can_start_dock_task,
    can_stop_cleaning,
    can_stop_dock_task,
    dock_task,
    is_narwal_task_busy,
)
from .entity import NarwalDockEntity, NarwalEntity, is_dock_consumable_name
from .narwal_client import CommandResponse, CommandResult, WorkingStatus


@dataclass(frozen=True, kw_only=True)
class NarwalButtonEntityDescription(ButtonEntityDescription):
    """Description for a Narwal action button."""

    action: str
    icon: str


@dataclass(frozen=True, kw_only=True)
class NarwalRobotButtonEntityDescription(ButtonEntityDescription):
    """Description for a robot/vacuum command button."""

    icon: str


@dataclass(frozen=True, kw_only=True)
class NarwalConsumableInfoResetDescription:
    """Description for clearing a robot-reported consumable alert item."""

    key: str
    name: str
    icon: str
    maintain_items: tuple[int, ...] = ()
    replace_items: tuple[int, ...] = ()


BUTTON_DESCRIPTIONS: tuple[NarwalButtonEntityDescription, ...] = (
    NarwalButtonEntityDescription(
        key="stop_dock_task",
        translation_key="stop_dock_task",
        action="stop_dock_task",
        icon="mdi:stop-circle-outline",
    ),
    NarwalButtonEntityDescription(
        key="empty_dustbin",
        translation_key="empty_dustbin",
        action="empty_dustbin",
        icon="mdi:delete-empty",
    ),
    NarwalButtonEntityDescription(
        key="wash_mop",
        translation_key="wash_mop",
        action="wash_mop",
        icon="mdi:waves-arrow-up",
    ),
    NarwalButtonEntityDescription(
        key="dry_mop",
        translation_key="dry_mop",
        action="dry_mop",
        icon="mdi:fan",
    ),
    NarwalButtonEntityDescription(
        key="dry_dust_bin",
        translation_key="dry_dust_bin",
        action="dry_dust_bag",
        icon="mdi:air-filter",
    ),
    NarwalButtonEntityDescription(
        key="dry_dock_bag",
        translation_key="dry_dock_bag",
        action="dry_station_bag",
        icon="mdi:shield-sun-outline",
    ),
)

ROBOT_BUTTON_DESCRIPTIONS: tuple[NarwalRobotButtonEntityDescription, ...] = (
    NarwalRobotButtonEntityDescription(
        key="start_cleaning",
        translation_key="start_cleaning",
        icon="mdi:play",
    ),
    NarwalRobotButtonEntityDescription(
        key="resume_cleaning",
        translation_key="resume_cleaning",
        icon="mdi:play",
    ),
    NarwalRobotButtonEntityDescription(
        key="pause_cleaning",
        translation_key="pause_cleaning",
        icon="mdi:pause",
    ),
    NarwalRobotButtonEntityDescription(
        key="stop_cleaning",
        translation_key="stop_cleaning",
        icon="mdi:stop",
    ),
    NarwalRobotButtonEntityDescription(
        key="return_to_dock",
        translation_key="return_to_dock",
        icon="mdi:home-import-outline",
    ),
    NarwalRobotButtonEntityDescription(
        key="locate_robot",
        translation_key="locate_robot",
        icon="mdi:map-marker-radius",
    ),
)


CONSUMABLE_CLEAR_ICONS: dict[str, str] = {
    "anti-winding brush": "mdi:broom",
    "cliff sensor": "mdi:radar",
    "detergent": "mdi:cup-water",
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


def _maintenance_reset_descriptions() -> tuple[NarwalConsumableInfoResetDescription, ...]:
    """Return reset button descriptions for robot consumable alert lists."""
    descriptions: list[NarwalConsumableInfoResetDescription] = []
    for code, item in sorted(CONSUMABLE_MAINTAIN_ITEMS.items()):
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"maintenance_{slugify(item)}_clear",
                name=f"{_friendly_item_name(item)} maintenance clear",
                icon=CONSUMABLE_CLEAR_ICONS.get(item, "mdi:wrench-clock"),
                maintain_items=(code,),
            )
        )
    for code, item in sorted(CONSUMABLE_REPLACE_ITEMS.items()):
        if item in AUTOCLEAR_REPLACE_ITEMS:
            continue
        descriptions.append(
            NarwalConsumableInfoResetDescription(
                key=f"replacement_{slugify(item)}_clear",
                name=f"{_friendly_item_name(item)} replacement clear",
                icon=CONSUMABLE_CLEAR_ICONS.get(item, "mdi:package-variant-closed"),
                replace_items=(code,),
            )
        )
    return tuple(descriptions)


CONSUMABLE_INFO_RESET_DESCRIPTIONS = _maintenance_reset_descriptions()


STATION_TASK_LABELS: dict[str, str] = {
    "emptying_dustbin": "Emptying dustbin",
    "washing_mop": "Washing mop",
    "drying_mop": "Drying mop",
    "drying_or_disinfecting": "Drying / disinfecting",
    "station_active": "Station active",
}


def _station_task(state) -> str | None:
    """Return the active dock task."""
    return dock_task(state)


def _format_duration(seconds: int) -> str:
    """Return a short human-readable duration."""
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal button entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalRobotActionButton(coordinator, description)
        for description in ROBOT_BUTTON_DESCRIPTIONS
    )
    async_add_entities(
        NarwalActionButton(coordinator, description)
        for description in BUTTON_DESCRIPTIONS
    )
    known_consumable_info_resets: set[str] = set()

    @callback
    def async_add_consumable_info_reset_buttons() -> None:
        state = coordinator.data or coordinator.client.state
        if state is None or not getattr(state, "consumable_info_available", False):
            return
        maintain_items = set(getattr(state, "maintain_items", ()))
        replace_items = set(getattr(state, "replace_items", ()))
        new_descriptions = sorted(
            (
                description
                for description in CONSUMABLE_INFO_RESET_DESCRIPTIONS
                if description.key not in known_consumable_info_resets
                and (
                    maintain_items.intersection(description.maintain_items)
                    or replace_items.intersection(description.replace_items)
                )
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


class NarwalRobotActionButton(NarwalEntity, ButtonEntity):
    """Button entity for a robot/vacuum command."""

    entity_description: NarwalRobotButtonEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalRobotButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon

    @property
    def available(self) -> bool:
        """Return True when this robot command can be used."""
        if not super().available:
            return False
        return self._command_available(self.coordinator.data)

    def _command_available(self, state) -> bool:
        """Return True when this robot command is valid for a state snapshot."""
        if self.entity_description.key == "start_cleaning":
            return can_start_cleaning(state) or self._can_wake_unknown_for_start(state)
        if self.entity_description.key == "resume_cleaning":
            return can_resume_cleaning(state)
        if self.entity_description.key == "pause_cleaning":
            return can_pause_cleaning(state)
        if self.entity_description.key == "stop_cleaning":
            return can_stop_cleaning(state)
        if self.entity_description.key == "return_to_dock":
            return can_return_home(state)
        if self.entity_description.key == "locate_robot":
            return can_locate_robot(state)
        return False

    @staticmethod
    def _can_wake_unknown_for_start(state) -> bool:
        """Allow startup-unknown robots to wake before enforcing start validity."""
        return (
            state is not None
            and state.working_status == WorkingStatus.UNKNOWN
            and not is_narwal_task_busy(state)
        )

    def _fresh_command_available(self, state) -> bool:
        """Return True when a refreshed state permits executing the command."""
        if self.entity_description.key == "start_cleaning":
            return can_start_cleaning(state)
        return self._command_available(state)

    async def async_press(self) -> None:
        """Run the robot command."""
        if not self.available:
            raise HomeAssistantError("Narwal robot command is not available")

        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)

        if not self._fresh_command_available(client.state):
            raise HomeAssistantError("Narwal robot command is not available")

        key = self.entity_description.key
        if key == "start_cleaning":
            response = await self._start_cleaning()
        elif key == "resume_cleaning":
            response = await client.resume(timeout=10.0)
        elif key == "pause_cleaning":
            response = await client.pause()
        elif key == "stop_cleaning":
            response = await client.stop()
            if response.success:
                self.coordinator.set_active_room_ids(None)
        elif key == "return_to_dock":
            response = await client.return_to_base(timeout=10.0)
        elif key == "locate_robot":
            response = await client.locate()
        else:
            raise HomeAssistantError(f"Unsupported Narwal robot command: {key}")

        if not response.success and response.result_code != 0:
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal {self.entity_description.key} failed: {result_name}"
            )
        self.coordinator.async_set_updated_data(client.state)

    async def _start_cleaning(self) -> CommandResponse:
        """Start a whole-home clean using the known room list where possible."""
        client = self.coordinator.client
        room_ids = await self._all_room_ids()
        if not self._fresh_command_available(client.state):
            raise HomeAssistantError("Narwal robot command is not available")
        if room_ids:
            settings = self.coordinator.clean_settings
            response = await client.start_rooms(
                room_ids,
                work_mode=settings.work_mode,
                fan=settings.fan,
                water=settings.water,
                mop_strength=settings.mop_strength,
                passes=settings.passes,
                route=settings.route,
                room_settings=self.coordinator.room_clean_settings_for_rooms(room_ids),
            )
        else:
            response = await client.start_rooms([])
        if response.success and room_ids:
            self.coordinator.set_active_room_ids(room_ids)
        return response

    async def _all_room_ids(self) -> list[int]:
        """Return every cleanable room id for a whole-home clean."""
        state = self.coordinator.data or self.coordinator.client.state
        if state is None or state.map_data is None:
            with suppress(Exception):
                await self.coordinator.client.get_map()
            state = self.coordinator.data or self.coordinator.client.state
            if state is None or state.map_data is None:
                state = self.coordinator.client.state
        if state and state.map_data:
            return [room.room_id for room in state.map_data.rooms if room.room_id > 0]
        return []


class NarwalActionButton(NarwalDockEntity, ButtonEntity):
    """Button entity for a dock/station maintenance command."""

    entity_description: NarwalButtonEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon

    @property
    def available(self) -> bool:
        """Return True when the integration can send station actions."""
        if not super().available:
            return False
        state = self.coordinator.data
        if self.entity_description.key != "stop_dock_task":
            return can_start_dock_task(state)
        return can_stop_dock_task(state)

    @property
    def extra_state_attributes(self) -> dict[str, int | str | bool] | None:
        """Return dock task context for the station stop button."""
        if self.entity_description.key != "stop_dock_task":
            return None

        state = self.coordinator.data
        if state is None:
            return None

        attributes: dict[str, int | str | bool] = {
            "dock_active": state.is_station_active,
            "docked": state.is_docked,
        }
        if station_task := _station_task(state):
            attributes["station_task"] = station_task
            attributes["task"] = STATION_TASK_LABELS.get(station_task, station_task)
        if state.dry_mop_remaining_time is not None:
            attributes["drying_time_left"] = state.dry_mop_remaining_time
            attributes["time_left"] = _format_duration(state.dry_mop_remaining_time)
        if state.mop_drying_target > 0:
            attributes["mop_drying_elapsed"] = state.mop_drying_elapsed
            attributes["mop_drying_target"] = state.mop_drying_target
        return attributes

    async def async_press(self) -> None:
        """Run the Narwal station action."""
        if not self.available:
            raise HomeAssistantError("Narwal dock task is not available")

        client = self.coordinator.client
        state = self.coordinator.data
        if self.entity_description.key == "stop_dock_task":
            if not can_stop_dock_task(state):
                raise HomeAssistantError("Narwal dock task is not active")
        elif not can_start_dock_task(state):
            raise HomeAssistantError("Narwal dock task cannot be started right now")

        if not client.robot_awake:
            await client.wake(timeout=10.0)

        if self.entity_description.key == "stop_dock_task":
            if not can_stop_dock_task(client.state):
                raise HomeAssistantError("Narwal dock task is not active")
        elif not can_start_dock_task(client.state):
            raise HomeAssistantError("Narwal dock task cannot be started right now")

        command: Callable[[], Awaitable[CommandResponse]] = getattr(
            client,
            self.entity_description.action,
        )
        response = await command()
        if self.entity_description.action == "wash_mop" and response.not_applicable:
            response = await client.wash_mop_by_robot_status()
        if self.entity_description.action == "dry_mop" and response.success:
            client.state.last_dry_mop_empty_time = 0.0
        if not response.success and response.result_code != 0:
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal {self.entity_description.key} failed: {result_name}"
            )

        self.coordinator.async_set_updated_data(client.state)


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
        self._attr_suggested_object_id = f"{title}_{description.key}"
        self._attr_name = description.name
        self._attr_icon = description.icon
        if is_dock_consumable_name(description.name):
            self._use_dock_device_info()

    @property
    def available(self) -> bool:
        """Return True when this alert clear button has an active item to clear."""
        if not super().available:
            return False
        state = self.coordinator.data
        if state is None:
            return False
        if not state.consumable_info_available:
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
        if not response.success:
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal consumable alert clear failed: {result_name}"
            )

        if client.state.consumable_info_available and (
            set(self.description.maintain_items).intersection(client.state.maintain_items)
            or set(self.description.replace_items).intersection(client.state.replace_items)
        ):
            raise HomeAssistantError("Narwal consumable alert did not clear")

        self.coordinator.async_set_updated_data(client.state)
