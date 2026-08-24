"""Switch entities for Narwal dock tasks and map display options."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
)
from .coordinator import (
    NarwalCoordinator,
    can_start_dock_task,
    can_stop_dock_task,
    dock_task,
    dock_task_key,
)
from .entity import NarwalDockEntity, NarwalEntity
from .narwal_client import CommandResponse, CommandResult

STATION_TASK_LABELS: dict[str, str] = {
    "emptying_dustbin": "Emptying dustbin",
    "washing_mop": "Washing mop",
    "drying_mop": "Drying mop",
    "dry_dust_bin": "Drying / disinfecting dust bin",
    "dry_dock_bag": "Drying / disinfecting dock bag",
    "drying_or_disinfecting": "Drying / disinfecting",
    "station_active": "Station active",
}


@dataclass(frozen=True, kw_only=True)
class NarwalDockTaskSwitchEntityDescription(SwitchEntityDescription):
    """Description for a Narwal dock task switch."""

    action: str
    icon: str


DOCK_TASK_SWITCHES: tuple[NarwalDockTaskSwitchEntityDescription, ...] = (
    NarwalDockTaskSwitchEntityDescription(
        key="empty_dustbin",
        translation_key="empty_dustbin",
        action="empty_dustbin",
        icon="mdi:delete-empty",
    ),
    NarwalDockTaskSwitchEntityDescription(
        key="wash_mop",
        translation_key="wash_mop",
        action="wash_mop",
        icon="mdi:waves-arrow-up",
    ),
    NarwalDockTaskSwitchEntityDescription(
        key="dry_mop",
        translation_key="dry_mop",
        action="dry_mop",
        icon="mdi:fan",
    ),
    NarwalDockTaskSwitchEntityDescription(
        key="dry_dust_bin",
        translation_key="dry_dust_bin",
        action="dry_dust_bag",
        icon="mdi:air-filter",
    ),
    NarwalDockTaskSwitchEntityDescription(
        key="dry_dock_bag",
        translation_key="dry_dock_bag",
        action="dry_station_bag",
        icon="mdi:shield-sun-outline",
    ),
)


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
        NarwalDockTaskSwitch(coordinator, description)
        for description in DOCK_TASK_SWITCHES
    )
    async_add_entities(
        NarwalMapOptionSwitch(coordinator, description)
        for description in MAP_SWITCHES
    )


def _format_duration(seconds: int) -> str:
    """Return a short human-readable duration."""
    minutes = _duration_minutes(seconds)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _duration_minutes(seconds: int) -> int:
    """Return remaining duration rounded up to whole minutes."""
    if seconds <= 0:
        return 0
    return (seconds + 59) // 60


class NarwalDockTaskSwitch(NarwalDockEntity, SwitchEntity):
    """Stateful start/stop control for one Narwal dock task."""

    entity_description: NarwalDockTaskSwitchEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalDockTaskSwitchEntityDescription,
    ) -> None:
        """Initialize the dock task switch."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon

    @property
    def is_on(self) -> bool | None:
        """Return whether this dock task is active."""
        state = self.coordinator.data
        if state is None:
            return None
        return dock_task_key(state) == self.entity_description.key

    @property
    def available(self) -> bool:
        """Return True when this dock task can be started or stopped."""
        if not super().available:
            return False
        state = self.coordinator.data
        if self.is_on:
            return can_stop_dock_task(state)
        return can_start_dock_task(state)

    @property
    def extra_state_attributes(self) -> dict[str, int | str | bool] | None:
        """Return dock task status and progress attributes."""
        state = self.coordinator.data
        if state is None:
            return None

        raw_task = dock_task(state)
        attributes: dict[str, int | str | bool] = {
            "dock_active": state.is_station_active,
            "docked": state.is_docked,
            "active": self.is_on is True,
        }
        if raw_task is not None:
            attributes["raw_task"] = raw_task
            attributes["task"] = STATION_TASK_LABELS.get(raw_task, raw_task)
        remaining = (
            state.dock_drying_remaining_time
            if state.dock_drying_remaining_time is not None
            else state.dry_mop_remaining_time
        )
        if remaining is not None:
            attributes["time_left"] = _format_duration(remaining)
            attributes["time_left_minutes"] = _duration_minutes(remaining)
        if state.dock_drying_timer_fields is not None:
            attributes["timer_fields"] = "/".join(state.dock_drying_timer_fields)
        if state.dock_drying_target > 0:
            elapsed = max(0, state.dock_drying_elapsed)
            target = state.dock_drying_target
        else:
            elapsed = max(0, state.mop_drying_elapsed)
            target = state.mop_drying_target
        if target > 0:
            attributes["elapsed_minutes"] = _duration_minutes(elapsed)
            attributes["target_minutes"] = _duration_minutes(target)
            attributes["progress"] = min(100, round(elapsed / target * 100))
        return attributes

    async def async_turn_on(self, **kwargs) -> None:
        """Start this dock task."""
        if self.is_on:
            return
        if not self.available:
            raise HomeAssistantError("Narwal dock task cannot be started right now")

        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)
        if not can_start_dock_task(client.state):
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
        self._raise_if_command_failed(response, "start")

        await self.coordinator.async_refresh_dock_status()

    async def async_turn_off(self, **kwargs) -> None:
        """Stop this dock task."""
        if not self.is_on:
            return
        if not can_stop_dock_task(self.coordinator.data):
            raise HomeAssistantError("Narwal dock task cannot be stopped right now")

        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)
        if not can_stop_dock_task(client.state):
            raise HomeAssistantError("Narwal dock task cannot be stopped right now")

        response = await client.stop_dock_task()
        self._raise_if_command_failed(response, "stop")

        await self.coordinator.async_refresh_dock_status()

    def _raise_if_command_failed(self, response: CommandResponse, action: str) -> None:
        """Raise a Home Assistant service error for rejected dock commands."""
        if response.success or response.result_code == 0:
            return
        try:
            result_name = CommandResult(response.result_code).name
        except ValueError:
            result_name = f"UNKNOWN({response.result_code})"
        raise HomeAssistantError(
            f"Narwal {action} {self.entity_description.key} failed: {result_name}"
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
