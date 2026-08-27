"""Switch entities for Narwal dock tasks and map display options."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import (
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
)
from .coordinator import NarwalCoordinator
from .dock_tasks import DOCK_TASKS, can_start_dock_task, can_stop_dock_task
from .entity import NarwalDockEntity, NarwalEntity
from .narwal_client import CommandResponse, CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalDockTaskSwitchEntityDescription(SwitchEntityDescription):
    """Description for a Narwal dock task switch."""

    action: str
    icon: str


DOCK_TASK_SWITCHES: tuple[NarwalDockTaskSwitchEntityDescription, ...] = tuple(
    NarwalDockTaskSwitchEntityDescription(
        key=task.key,
        translation_key=task.translation_key,
        action=task.action,
        icon=task.icon,
    )
    for task in DOCK_TASKS
)
RESTORED_DOCK_TASK_ASSUME_TTL = 6 * 60 * 60


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


def _accepted_response(response: CommandResponse) -> bool:
    """Return true for response codes that mean the robot accepted a command."""
    return response.accepted


class NarwalDockTaskSwitch(NarwalDockEntity, RestoreEntity, SwitchEntity):
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

    async def async_added_to_hass(self) -> None:
        """Restore a running dock task reservation until fresh status corrects it."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self.coordinator.client.state.assume_dock_task(
                self.entity_description.key,
                ttl=RESTORED_DOCK_TASK_ASSUME_TTL,
            )

    @property
    def is_on(self) -> bool | None:
        """Return whether this dock task is active."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.key in state.active_dock_task_keys

    @property
    def available(self) -> bool:
        """Return True when this dock task can be started or stopped."""
        if not super().available or not self.coordinator.has_fresh_state:
            return False
        state = self.coordinator.data
        if self.is_on:
            return True
        return can_start_dock_task(state, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, str | int] | None:
        """Return task progress attributes from typed dock telemetry."""
        state = self.coordinator.data
        if state is None or not self.is_on:
            return None
        timer = state.dock_task_timer(self.entity_description.key)
        if timer is None:
            return None
        return {
            "time_left": _format_duration(timer.remaining),
            "progress": timer.progress_percent,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Start this dock task."""
        async with self.coordinator.dock_action_lock:
            if self.is_on:
                return
            if not self.available:
                raise HomeAssistantError("Narwal dock task cannot be started right now")

            client = self.coordinator.client
            if not client.robot_awake:
                await client.wake(timeout=10.0)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if not can_start_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be started right now")

            command: Callable[[], Awaitable[CommandResponse]] = getattr(
                client,
                self.entity_description.action,
            )
            response = await command()
            self._raise_if_command_failed(response, "start")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    async def async_turn_off(self, **kwargs) -> None:
        """Stop this dock task."""
        async with self.coordinator.dock_action_lock:
            if not self.is_on:
                return
            if not can_stop_dock_task(self.coordinator.data, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be stopped right now")

            client = self.coordinator.client
            if not client.robot_awake:
                await client.wake(timeout=10.0)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if not can_stop_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be stopped right now")

            response = await client.stop_dock_task(self.entity_description.key)
            self._raise_if_command_failed(response, "stop")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    def _raise_if_command_failed(self, response: CommandResponse, action: str) -> None:
        """Raise a Home Assistant service error for rejected dock commands."""
        if _accepted_response(response):
            return
        try:
            result_name = CommandResult(response.result_code).name
        except (TypeError, ValueError):
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
