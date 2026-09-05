"""Narwal Flow Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TypeAlias

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAN_ROOMS,
    configured_model_name,
    fan_speed_map_for,
)
from .coordinator import NarwalCoordinator, can_prepare_clean_start
from .narwal_client import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalConnectionError,
    RoomCleanSettings,
    WorkMode,
)

_LOGGER = logging.getLogger(__name__)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

_CONFIG_ENTRY_MINOR_VERSION = 2
_LEGACY_REPLACED_SENSOR_SUFFIXES = (
    "base_station_cleaning_filter_used_hours",
    "current_room",
    "map_metadata",
    "status",
    "task_progress",
    "task_status",
)

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
SUCTION_OPTIONS = (
    "ai",
    "quiet",
    "standard",
    "normal",
    "strong",
    "super_powerful",
    "ultra_powerful",
    "super",
    "ultra",
    "max",
)
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
        vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
        vol.Optional(ATTR_DEVICE_ID): cv.ensure_list,
        vol.Optional(ATTR_AREA_ID): cv.ensure_list,
        vol.Required(FIELD_ROOMS): cv.ensure_list,
        vol.Optional(FIELD_MODE, default="vacuum_and_mop"): vol.In(WORK_MODE_OPTIONS),
        vol.Optional(FIELD_SUCTION, default="standard"): vol.In(SUCTION_OPTIONS),
        vol.Optional(FIELD_WATER, default="normal"): vol.In(WATER_OPTIONS),
        vol.Optional(FIELD_MOP_STRENGTH, default="normal"): vol.In(
            MOP_STRENGTH_OPTIONS
        ),
        vol.Optional(FIELD_PASSES, default=1): lambda value: _validate_pass_count(
            value
        ),
        vol.Optional(FIELD_ROUTE): vol.In(ROUTE_OPTIONS),
    }
)

SUCTION_OPTION_LABELS: dict[str, str] = {
    "quiet": "Quiet",
    "standard": "Standard",
    "normal": "Standard",
    "strong": "Strong",
    "ultra": "Ultra Powerful",
    "ultra_powerful": "Ultra Powerful",
    "super": "Super Powerful",
    "super_powerful": "Super Powerful",
    "max": "Super Powerful",
}


def _suction_for_coordinator(
    coordinator: NarwalCoordinator,
    option: str,
) -> FanLevel:
    """Return the model-valid suction level for a service option."""
    if option == "ai":
        return FanLevel.UNSPECIFIED
    fan_map = fan_speed_map_for(coordinator.config_entry.data, include_aliases=False)
    if option == "max":
        return list(fan_map.values())[-1]
    compatibility_label = {
        "super": "Super",
        "ultra": "Ultra",
    }.get(option)
    if compatibility_label is not None:
        compatibility_level = fan_speed_map_for(
            coordinator.config_entry.data
        ).get(compatibility_label)
        if compatibility_level is None:
            raise HomeAssistantError(
                f"{compatibility_label} suction is not supported by this Narwal model"
            )
        return compatibility_level
    label = SUCTION_OPTION_LABELS[option]
    if label not in fan_map:
        raise HomeAssistantError(
            f"{label} suction is not supported by this Narwal model"
        )
    return fan_map[label]


def _domain_data(hass: HomeAssistant) -> dict[str, NarwalCoordinator]:
    """Return Narwal domain runtime data."""
    return hass.data.setdefault(DOMAIN, {})


def _validate_pass_count(value: object) -> int:
    """Return a pass count, rejecting fractional coercion."""
    if isinstance(value, bool):
        raise vol.Invalid("Narwal pass count must be an integer")
    if isinstance(value, int):
        passes = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(text)
    else:
        raise vol.Invalid("Narwal pass count must be an integer")
    if passes < 1 or passes > 3:
        raise vol.Invalid("Narwal pass count must be between 1 and 3")
    return passes


def _room_tokens(raw_rooms: list) -> list[object]:
    """Return room request tokens split the same way for all/all+room checks."""
    tokens: list[object] = []
    for item in raw_rooms:
        if isinstance(item, str):
            cleaned = item.strip().strip("[]")
            tokens.extend(cleaned.replace(",", " ").split())
        else:
            tokens.append(item)
    return tokens


def _normalise_room_ids(raw_rooms: list) -> list[int]:
    """Return room IDs from HA service data."""
    room_ids: list[int] = []
    seen: set[int] = set()
    for value in _room_tokens(raw_rooms):
        if isinstance(value, str) and value.lower() == "all":
            continue
        if isinstance(value, bool):
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}")
        try:
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError
                room_id = int(value)
            elif isinstance(value, int):
                room_id = value
            elif isinstance(value, str) and value.isdecimal():
                room_id = int(value)
            else:
                raise ValueError
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}") from err
        if room_id <= 0:
            raise HomeAssistantError("Narwal room IDs must be positive")
        if room_id not in seen:
            seen.add(room_id)
            room_ids.append(room_id)
    return room_ids


def _rooms_requested_all(raw_rooms: list) -> bool:
    """Return True if a service call requests all current map rooms."""
    return any(
        isinstance(item, str) and item.lower() == "all"
        for item in _room_tokens(raw_rooms)
    )


async def _async_room_ids_for_coordinator(
    coordinator: NarwalCoordinator,
    raw_rooms: list,
) -> list[int]:
    """Resolve configured service rooms for a single Narwal coordinator."""
    requested_all = _rooms_requested_all(raw_rooms)
    tokens = _room_tokens(raw_rooms)
    if requested_all and len(tokens) > 1:
        raise HomeAssistantError('Use either "all" or explicit room IDs, not both')
    state = coordinator.client.state
    try:
        await coordinator.client.get_map()
    except Exception as err:
        raise HomeAssistantError("Narwal map could not be refreshed") from err
    if state.map_data is None:
        raise HomeAssistantError("Narwal map is not available")
    known_room_ids = {room.room_id for room in state.map_data.rooms if room.room_id > 0}
    if requested_all:
        return sorted(known_room_ids)

    room_ids = _normalise_room_ids(raw_rooms)
    unknown_ids = [room_id for room_id in room_ids if room_id not in known_room_ids]
    if unknown_ids:
        raise HomeAssistantError(
            f"Unknown Narwal room ID: {', '.join(str(room_id) for room_id in unknown_ids)}"
        )
    return room_ids


async def _async_validate_target_permissions(
    hass: HomeAssistant,
    call: ServiceCall,
    entity_ids: list[str],
) -> None:
    """Validate control permission for service target entities."""
    user_id = call.context.user_id
    if not user_id:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if user.is_admin:
        return
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )


async def _async_validate_clean_rooms_targets(
    hass: HomeAssistant,
    call: ServiceCall,
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
            and hass.states.get(entity_id) is not None
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

    await _async_validate_target_permissions(hass, call, vacuum_entity_ids)
    return vacuum_entity_ids


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
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        coordinator = data.get(registry_entry.config_entry_id)
        if not isinstance(coordinator, NarwalCoordinator):
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        if coordinator not in coordinators:
            coordinators.append(coordinator)
    if not coordinators:
        raise HomeAssistantError("Target does not contain a Narwal entity")
    return coordinators


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Narwal domain services."""
    if hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS):
        return

    async def _async_extract_entity_ids(call: ServiceCall) -> list[str]:
        """Extract target entity IDs across supported HA helper signatures."""
        try:
            entity_ids = await service.async_extract_entity_ids(call)
        except TypeError:
            entity_ids = await service.async_extract_entity_ids(hass, call)
        return list(entity_ids)

    async def async_clean_rooms(call: ServiceCall) -> None:
        entity_ids = await _async_extract_entity_ids(call)
        if not entity_ids and any(
            key in call.data for key in (ATTR_ENTITY_ID, ATTR_DEVICE_ID, ATTR_AREA_ID)
        ):
            raise HomeAssistantError("Target does not contain a Narwal entity")
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        coordinator = coordinators[0]

        work_mode = WORK_MODE_OPTIONS[call.data[FIELD_MODE]]
        water = WATER_OPTIONS[call.data[FIELD_WATER]]
        mop_strength = MOP_STRENGTH_OPTIONS[call.data[FIELD_MOP_STRENGTH]]
        passes = call.data[FIELD_PASSES]
        route_label = call.data.get(FIELD_ROUTE)

        if not coordinator.client.robot_awake:
            await coordinator.client.wake(timeout=10.0)

        room_ids = await _async_room_ids_for_coordinator(
            coordinator,
            call.data[FIELD_ROOMS],
        )
        if not room_ids:
            raise HomeAssistantError("At least one room must be selected")

        fan = _suction_for_coordinator(coordinator, call.data[FIELD_SUCTION])
        route = ROUTE_OPTIONS.get(route_label, coordinator.clean_settings.route)
        requested_settings = RoomCleanSettings(
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
        )
        room_settings = coordinator.room_clean_settings_for_rooms(
            room_ids,
            default=requested_settings,
            use_room_profiles=False,
        )

        async with coordinator.dock_action_lock:
            if not await coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if not can_prepare_clean_start(coordinator.client.state):
                raise HomeAssistantError("Narwal room clean cannot be started right now")
            if not await coordinator.async_prepare_clean_start():
                raise HomeAssistantError("Narwal room clean cannot be started right now")

            client = coordinator.client
            resp = await client.start_rooms(
                room_ids,
                work_mode=requested_settings.work_mode,
                fan=requested_settings.fan,
                water=requested_settings.water,
                mop_strength=requested_settings.mop_strength,
                passes=requested_settings.passes,
                route=requested_settings.route,
                room_settings=room_settings,
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
            if not resp.accepted:
                raise HomeAssistantError(
                    f"Narwal room clean failed: {result_name} ({resp.result_code})"
                )
            coordinator.clean_settings.work_mode = requested_settings.work_mode
            coordinator.clean_settings.fan = requested_settings.fan
            coordinator.clean_settings.water = requested_settings.water
            coordinator.clean_settings.mop_strength = requested_settings.mop_strength
            coordinator.clean_settings.passes = requested_settings.passes
            coordinator.clean_settings.route = requested_settings.route
            coordinator.record_accepted_clean_start(room_settings)
            client.state.assume_robot_clean()
            await coordinator.async_clear_map_display_cache()
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
    """Migrate old config entries and remove replaced status sensors."""
    update_kwargs: dict[str, object] = {}

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
        update_kwargs["data"] = new_data
        update_kwargs["version"] = 2
        _LOGGER.info("Migration complete: product_key=%s", new_data[CONF_PRODUCT_KEY])

    if getattr(config_entry, "minor_version", 1) < _CONFIG_ENTRY_MINOR_VERSION:
        _async_remove_legacy_replaced_sensors(hass, config_entry)
        update_kwargs["minor_version"] = _CONFIG_ENTRY_MINOR_VERSION

    if update_kwargs:
        hass.config_entries.async_update_entry(config_entry, **update_kwargs)
    return True


def _async_remove_legacy_replaced_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> None:
    """Remove old sensors now represented by native entities or attributes."""
    device_id = config_entry.data.get(CONF_DEVICE_ID)
    if not device_id:
        return

    registry = er.async_get(hass)
    unique_ids = {
        f"{device_id}_{suffix}" for suffix in _LEGACY_REPLACED_SENSOR_SUFFIXES
    }
    entity_ids: set[str] = set()

    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            unique_id,
        )
        if entity_id is not None:
            entity_ids.add(entity_id)

    for entry in er.async_entries_for_config_entry(
        registry,
        config_entry.entry_id,
    ):
        if (
            entry.domain == "sensor"
            and entry.platform == DOMAIN
            and entry.unique_id in unique_ids
        ):
            entity_ids.add(entry.entity_id)

    for entity_id in sorted(entity_ids):
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.platform != DOMAIN:
            continue
        _LOGGER.info("Removing legacy Narwal sensor %s", entity_id)
        registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    _async_remove_legacy_replaced_sensors(hass, entry)
    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Narwal vacuum at {entry.data['host']}: {err}"
        ) from err

    entry.runtime_data = coordinator
    _domain_data(hass)[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, entry.data["device_id"])}
    )
    if device is not None:
        device_registry.async_update_device(
            device.id,
            model=configured_model_name(entry.data),
            sw_version=coordinator.client.state.firmware_version or None,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()
        _domain_data(hass).pop(entry.entry_id, None)

    return unload_ok
