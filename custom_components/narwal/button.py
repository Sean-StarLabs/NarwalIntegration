"""Button entities for Narwal station maintenance actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalButtonEntityDescription(ButtonEntityDescription):
    """Description for a Narwal action button."""

    action: str
    icon: str


BUTTON_DESCRIPTIONS: tuple[NarwalButtonEntityDescription, ...] = (
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
        key="wash_and_dry_mop",
        translation_key="wash_and_dry_mop",
        action="wash_and_dry_mop",
        icon="mdi:creation",
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


class NarwalActionButton(NarwalEntity, ButtonEntity):
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
        return super().available

    async def async_press(self) -> None:
        """Run the Narwal station action."""
        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)

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
