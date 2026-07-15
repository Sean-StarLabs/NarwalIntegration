"""Narwal Flow Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TypeAlias

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAN_ROOMS,
)
from .coordinator import NarwalCoordinator
from .narwal_client import (
    CommandResult,
    CleaningRoute,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalConnectionError,
    WorkMode,
)

_LOGGER = logging.getLogger(__name__)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

FIELD_ROOMS = "rooms"
FIELD_MODE = "mode"
FIELD_SUCTION = "suction"
FIELD_WATER = "water"
FIELD_MOP_STRENGTH = "mop_strength"
FIELD_PASSES = "passes"
FIELD_ROUTE = "route"

WORK_MODE_OPTIONS: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
SUCTION_OPTIONS: dict[str, FanLevel] = {
    "ai": FanLevel.UNSPECIFIED,
    "quiet": FanLevel.MUTE,
    "standard": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "super_powerful": FanLevel.DEEP,
    "ultra_powerful": FanLevel.SUPER,
}
WATER_OPTIONS: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_OPTIONS: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}
ROUTE_OPTIONS: dict[str, CleaningRoute] = {
    "standard": CleaningRoute.STANDARD,
    "meticulous": CleaningRoute.METICULOUS,
}

CLEAN_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_DEVICE_ID): cv.ensure_list,
        vol.Optional(ATTR_AREA_ID): cv.ensure_list,
        vol.Required(FIELD_ROOMS): cv.ensure_list,
        vol.Optional(FIELD_MODE, default="vacuum_and_mop"): vol.In(WORK_MODE_OPTIONS),
        vol.Optional(FIELD_SUCTION, default="standard"): vol.In(SUCTION_OPTIONS),
        vol.Optional(FIELD_WATER, default="normal"): vol.In(WATER_OPTIONS),
        vol.Optional(FIELD_MOP_STRENGTH, default="normal"): vol.In(MOP_STRENGTH_OPTIONS),
        vol.Optional(FIELD_PASSES, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Optional(FIELD_ROUTE): vol.In(ROUTE_OPTIONS),
    }
)

def _normalise_room_ids(raw_rooms: list) -> list[int]:
    """Return room IDs from HA service data."""
    room_ids: list[int] = []
    for item in raw_rooms:
        if isinstance(item, str):
            cleaned = item.strip().strip("[]")
            if cleaned.lower() == "all":
                continue
            values = cleaned.replace(",", " ").split()
            room_ids.extend(int(value) for value in values if value)
        else:
            room_ids.append(int(item))
    return room_ids


def _rooms_requested_all(raw_rooms: list) -> bool:
    """Return True if a service call requests all current map rooms."""
    return any(isinstance(item, str) and item.strip().lower() == "all" for item in raw_rooms)


async def _async_room_ids_for_coordinator(
    coordinator: NarwalCoordinator,
    raw_rooms: list,
) -> list[int]:
    """Resolve configured service rooms for a single Narwal coordinator."""
    if not _rooms_requested_all(raw_rooms):
        return _normalise_room_ids(raw_rooms)

    state = coordinator.client.state
    if state.map_data is None:
        await coordinator.client.get_map()
    if state.map_data is None:
        return []
    return [room.room_id for room in state.map_data.rooms if room.room_id > 0]


def _domain_data(hass: HomeAssistant) -> dict:
    """Return Narwal domain runtime data."""
    return hass.data.setdefault(DOMAIN, {})


async def _async_get_service_coordinators(
    hass: HomeAssistant,
    entity_ids: list[str] | None,
) -> list[NarwalCoordinator]:
    """Resolve service entity IDs to Narwal coordinators."""
    data = _domain_data(hass)
    if not entity_ids:
        raise HomeAssistantError("Target a Narwal vacuum")

    registry = er.async_get(hass)
    coordinators: list[NarwalCoordinator] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.config_entry_id is None:
            continue
        coordinator = data.get(registry_entry.config_entry_id)
        if not isinstance(coordinator, NarwalCoordinator):
            continue
        if coordinator not in coordinators:
            coordinators.append(coordinator)
    if not coordinators:
        raise HomeAssistantError("Target does not contain a Narwal entity")
    return coordinators


async def _async_validate_clean_rooms_targets(
    hass: HomeAssistant,
    call,
    entity_ids: list[str],
) -> list[str]:
    """Validate clean-room target domains and user permissions."""
    registry = er.async_get(hass)
    vacuum_entity_ids: list[str] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if (
            entity_id.startswith(f"{VACUUM_DOMAIN}.")
            and registry_entry is not None
            and registry_entry.platform == DOMAIN
        ):
            vacuum_entity_ids.append(entity_id)

    direct_entity_ids = call.data.get(ATTR_ENTITY_ID, [])
    if isinstance(direct_entity_ids, str):
        direct_entity_ids = [direct_entity_ids]
    direct_entity_ids = [
        entity_id
        for entity_id in direct_entity_ids
        if entity_id != "all" and not entity_id.startswith("group.")
    ]
    if any(entity_id not in vacuum_entity_ids for entity_id in direct_entity_ids):
        raise HomeAssistantError("Target must be a Narwal vacuum entity")

    user_id = call.context.user_id
    if not user_id:
        return vacuum_entity_ids
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context, user_id=user_id)
    if user.is_admin:
        return vacuum_entity_ids
    for entity_id in vacuum_entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )
    return vacuum_entity_ids


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Narwal domain services."""

    async def async_clean_rooms(call) -> None:
        entity_ids = list(await service.async_extract_entity_ids(call))
        if not entity_ids and any(
            key in call.data for key in (ATTR_ENTITY_ID, ATTR_DEVICE_ID, ATTR_AREA_ID)
        ):
            raise HomeAssistantError("Target does not contain a Narwal entity")
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(
            hass,
            entity_ids,
        )
        for coordinator in coordinators:
            client = coordinator.client
            if not client.robot_awake:
                await client.wake(timeout=10.0)
            room_ids = await _async_room_ids_for_coordinator(
                coordinator,
                call.data[FIELD_ROOMS],
            )
            if not room_ids:
                raise HomeAssistantError("At least one room must be selected")
            resp = await client.start_rooms(
                room_ids,
                work_mode=WORK_MODE_OPTIONS[call.data[FIELD_MODE]],
                fan=SUCTION_OPTIONS[call.data[FIELD_SUCTION]],
                water=WATER_OPTIONS[call.data[FIELD_WATER]],
                mop_strength=MOP_STRENGTH_OPTIONS[call.data[FIELD_MOP_STRENGTH]],
                passes=call.data[FIELD_PASSES],
                route=ROUTE_OPTIONS[call.data[FIELD_ROUTE]]
                if FIELD_ROUTE in call.data
                else None,
            )
            if resp.result_code == 0:
                result_name = "ACCEPTED"
            else:
                try:
                    result_name = CommandResult(resp.result_code).name
                except ValueError:
                    result_name = f"UNKNOWN({resp.result_code})"
            _LOGGER.info(
                "Clean rooms response: %s (code=%s), rooms=%s",
                result_name,
                resp.result_code,
                room_ids,
            )
            if resp.result_code not in (0, CommandResult.SUCCESS):
                raise HomeAssistantError(
                    f"Narwal room clean failed: {result_name} ({resp.result_code})"
                )
            coordinator.async_set_updated_data(client.state)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAN_ROOMS,
        async_clean_rooms,
        schema=CLEAN_ROOMS_SCHEMA,
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Narwal services."""
    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to version 2 (add product_key)."""
    if config_entry.version < 2:
        _LOGGER.info(
            "Migrating Narwal config entry from version %d to 2",
            config_entry.version,
        )
        new_data = {**config_entry.data}
        if CONF_PRODUCT_KEY not in new_data:
            new_data[CONF_PRODUCT_KEY] = "QoEsI5qYXO"
        if CONF_MODEL not in new_data:
            new_data[CONF_MODEL] = "Narwal Flow"
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2,
        )
        _LOGGER.info("Migration complete: product_key=%s", new_data[CONF_PRODUCT_KEY])
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Narwal vacuum at {entry.data['host']}: {err}"
        ) from err

    entry.runtime_data = coordinator
    data = _domain_data(hass)
    data[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()
        data = _domain_data(hass)
        data.pop(entry.entry_id, None)

    return unload_ok
