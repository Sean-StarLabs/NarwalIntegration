"""Base entity for Narwal vacuum integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, configured_model_name
from .coordinator import NarwalCoordinator

_DOCK_CONSUMABLE_NAME_MARKERS = (
    "base station",
    "clean water",
    "clear water",
    "curing agent",
    "detergent",
    "dock",
    "dust bag",
    "heavy detergent",
    "sewage",
    "silver ion",
    "station",
    "wash ribs",
    "water tank sponge",
)

_DOCK_CONSUMABLE_CODE_MARKERS = (
    "base",
    "clean_water",
    "clear_water",
    "detergent",
    "dock",
    "dust_bag",
    "heavy_detergent",
    "sewage",
    "silver_ion",
    "station",
    "wash",
    "water_tank",
)


def narwal_robot_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Return device info for the robot vacuum."""
    device_id = coordinator.config_entry.data["device_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        model=configured_model_name(coordinator.config_entry.data),
        sw_version=coordinator.client.state.firmware_version or None,
        name=coordinator.config_entry.title,
    )


def narwal_dock_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Return device info for the robot dock/base station."""
    device_id = coordinator.config_entry.data["device_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, f"{device_id}_dock")},
        manufacturer=MANUFACTURER,
        model=f"{configured_model_name(coordinator.config_entry.data)} Dock",
        sw_version=coordinator.client.state.firmware_version or None,
        name=f"{coordinator.config_entry.title} Dock",
        via_device=(DOMAIN, device_id),
    )


def is_dock_consumable_name(name: str) -> bool:
    """Return true when a consumable belongs to the dock/base station."""
    normalized = name.casefold().replace("-", " ")
    return any(marker in normalized for marker in _DOCK_CONSUMABLE_NAME_MARKERS)


def is_dock_consumable_identity(code: str, name: str = "") -> bool:
    """Return true when stable consumable metadata identifies a dock item."""
    normalized_code = code.casefold().replace("-", "_").replace(" ", "_")
    return any(
        marker in normalized_code for marker in _DOCK_CONSUMABLE_CODE_MARKERS
    ) or is_dock_consumable_name(name)


class NarwalEntity(CoordinatorEntity[NarwalCoordinator]):
    """Base class for Narwal entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_device_info = narwal_robot_device_info(coordinator)

    def _use_dock_device_info(self) -> None:
        """Attach this entity to the dock/base-station device."""
        self._attr_device_info = narwal_dock_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        local_available = getattr(self.coordinator, "local_available", None)
        if isinstance(local_available, bool):
            return local_available
        return self.coordinator.last_update_success


class NarwalDockEntity(NarwalEntity):
    """Base class for Narwal dock/base-station entities."""

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the dock entity."""
        super().__init__(coordinator)
        self._use_dock_device_info()
