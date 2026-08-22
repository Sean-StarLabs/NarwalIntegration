"""Button entities for Narwal station maintenance actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .const import CONSUMABLE_MAINTAIN_ITEMS, CONSUMABLE_REPLACE_ITEMS
from .coordinator import NarwalCoordinator
from .entity import NarwalDockEntity, NarwalEntity, is_dock_consumable_name
from .narwal_client import CommandResponse, CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalButtonEntityDescription(ButtonEntityDescription):
    """Description for a Narwal action button."""

    action: str
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
    if not state.is_station_active:
        return None
    if state.station_activity == 1:
        return "emptying_dustbin"
    if state.is_washing_mop:
        return "washing_mop"
    if state.is_drying_mop:
        return "drying_mop"
    if state.station_activity == 4:
        return "drying_or_disinfecting"
    return "station_active"


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
        NarwalActionButton(coordinator, description)
        for description in BUTTON_DESCRIPTIONS
    )
    async_add_entities(
        NarwalConsumableInfoResetButton(coordinator, description)
        for description in CONSUMABLE_INFO_RESET_DESCRIPTIONS
    )


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
            return (
                state is not None
                and (state.is_docked or state.dock_state_unknown)
                and not state.is_station_active
            )
        return state is not None and state.is_station_active

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
        client = self.coordinator.client
        if self.entity_description.key == "stop_dock_task":
            state = self.coordinator.data
            if state is None or not state.is_station_active:
                raise HomeAssistantError("Narwal dock task is not active")

        if not client.robot_awake:
            await client.wake(timeout=10.0)

        if (
            self.entity_description.key == "stop_dock_task"
            and not client.state.is_station_active
        ):
            raise HomeAssistantError("Narwal dock task is not active")

        command: Callable[[], Awaitable[CommandResponse]] = getattr(
            client,
            self.entity_description.action,
        )
        response = await command()
        if self.entity_description.action == "wash_mop" and response.not_applicable:
            response = await client.wash_mop_by_robot_status()
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
        if response.result_code not in (0, CommandResult.SUCCESS):
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal consumable alert clear failed: {result_name}"
            )

        remaining = set(self.description.maintain_items).intersection(
            client.state.maintain_items
        ) or set(self.description.replace_items).intersection(client.state.replace_items)
        if remaining:
            raise HomeAssistantError("Narwal consumable alert did not clear")

        self.coordinator.async_set_updated_data(client.state)
