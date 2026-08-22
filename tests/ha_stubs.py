"""Minimal homeassistant module stubs for testing without HA installed.

Import this module BEFORE importing any custom_components code.
It injects mock HA modules into sys.modules so that custom_components
can be imported and tested in isolation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType
from unittest.mock import MagicMock

_INSTALLED = False


def install() -> None:
    """Install HA stubs into sys.modules. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    def _mod(name: str, parent: ModuleType | None = None) -> ModuleType:
        m = ModuleType(name)
        sys.modules[name] = m
        if parent is not None:
            attr = name.rsplit(".", 1)[-1]
            setattr(parent, attr, m)
        return m

    # --- voluptuous (HA dependency, not in our test requirements) ---
    vol = _mod("voluptuous")
    vol.Schema = MagicMock()  # type: ignore[attr-defined]
    vol.Required = MagicMock(side_effect=lambda *a, **kw: a[0] if a else "key")  # type: ignore[attr-defined]
    vol.Optional = MagicMock(side_effect=lambda *a, **kw: a[0] if a else "key")  # type: ignore[attr-defined]
    vol.In = MagicMock()  # type: ignore[attr-defined]

    # --- homeassistant ---
    ha = _mod("homeassistant")

    # homeassistant.const
    ha_const = _mod("homeassistant.const", ha)
    ha_const.Platform = MagicMock()  # type: ignore[attr-defined]
    ha_const.PERCENTAGE = "%"  # type: ignore[attr-defined]
    ha_const.UnitOfArea = MagicMock()  # type: ignore[attr-defined]
    ha_const.UnitOfTime = MagicMock()  # type: ignore[attr-defined]

    class _EntityCategory:
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class _UnitOfArea:
        SQUARE_METERS = "m²"

    class _UnitOfTime:
        SECONDS = "s"

    ha_const.EntityCategory = _EntityCategory  # type: ignore[attr-defined]
    ha_const.PERCENTAGE = "%"  # type: ignore[attr-defined]
    ha_const.UnitOfArea = _UnitOfArea  # type: ignore[attr-defined]
    ha_const.UnitOfTime = _UnitOfTime  # type: ignore[attr-defined]

    # homeassistant.core
    ha_core = _mod("homeassistant.core", ha)
    ha_core.HomeAssistant = MagicMock  # type: ignore[attr-defined]
    ha_core.callback = lambda f: f  # type: ignore[attr-defined]

    ha_util = _mod("homeassistant.util", ha)
    ha_util.slugify = lambda value: str(value).lower().replace(" ", "_")  # type: ignore[attr-defined]

    # homeassistant.auth.permissions.const
    ha_auth = _mod("homeassistant.auth", ha)
    ha_auth_permissions = _mod("homeassistant.auth.permissions", ha_auth)
    ha_auth_permissions_const = _mod(
        "homeassistant.auth.permissions.const", ha_auth_permissions
    )
    ha_auth_permissions_const.POLICY_CONTROL = "control"  # type: ignore[attr-defined]

    # homeassistant.exceptions
    ha_exc = _mod("homeassistant.exceptions", ha)
    ha_exc.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})  # type: ignore[attr-defined]
    ha_exc.HomeAssistantError = type(  # type: ignore[attr-defined]
        "HomeAssistantError", (Exception,), {}
    )

    # homeassistant.config_entries
    ha_ce = _mod("homeassistant.config_entries", ha)

    class _ConfigFlow:
        DOMAIN = ""
        VERSION = 1

        def __init_subclass__(cls, domain: str = "", **kw: object) -> None:
            cls.DOMAIN = domain

    ha_ce.ConfigFlow = _ConfigFlow  # type: ignore[attr-defined]
    ha_ce.ConfigFlowResult = dict  # type: ignore[attr-defined]
    class _ConfigEntry:
        """Subscriptable ConfigEntry stub for TypeAlias usage."""

        def __class_getitem__(cls, item: object) -> type:
            return cls

    ha_ce.ConfigEntry = _ConfigEntry  # type: ignore[attr-defined]

    # homeassistant.data_entry_flow
    ha_def = _mod("homeassistant.data_entry_flow", ha)

    class _AbortFlow(Exception):
        def __init__(self, reason: str) -> None:
            self.reason = reason
            super().__init__(reason)

    ha_def.AbortFlow = _AbortFlow  # type: ignore[attr-defined]

    # homeassistant.helpers (and sub-modules)
    ha_helpers = _mod("homeassistant.helpers", ha)

    # NOTE: deliberately does NOT expose EntityCategory. Real HA removed
    # homeassistant.helpers.entity.EntityCategory; it lives in homeassistant.const.
    # Stubbing both paths hid a real ImportError that only surfaced when the
    # integration was loaded in Home Assistant (switch.py, from PR #62).
    ha_entity = _mod("homeassistant.helpers.entity", ha_helpers)

    ha_uc = _mod("homeassistant.helpers.update_coordinator", ha_helpers)

    class _DataUpdateCoordinator:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def __class_getitem__(cls, item: object) -> type:
            return cls

    ha_uc.DataUpdateCoordinator = _DataUpdateCoordinator  # type: ignore[attr-defined]
    ha_uc.UpdateFailed = type("UpdateFailed", (Exception,), {})  # type: ignore[attr-defined]

    class _CoordinatorEntity:
        """Stub for CoordinatorEntity base class."""

        def __init__(self, coordinator: object) -> None:
            self.coordinator = coordinator

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def __class_getitem__(cls, item: object) -> type:
            return cls

        def async_write_ha_state(self) -> None:
            pass

        def _handle_coordinator_update(self) -> None:
            pass

    ha_uc.CoordinatorEntity = _CoordinatorEntity  # type: ignore[attr-defined]

    ha_dr = _mod("homeassistant.helpers.device_registry", ha_helpers)
    ha_dr.DeviceInfo = dict  # type: ignore[attr-defined]

    ha_ep = _mod("homeassistant.helpers.entity_platform", ha_helpers)
    ha_ep.AddConfigEntryEntitiesCallback = MagicMock  # type: ignore[attr-defined]

    # homeassistant.helpers.service_info.* — discovery payloads (zeroconf, DHCP)
    ha_si = _mod("homeassistant.helpers.service_info", ha_helpers)

    class _ZeroconfServiceInfo:
        """Stub for ZeroconfServiceInfo (only the fields the flow reads)."""

        def __init__(self, host: str = "", hostname: str = "", port: int = 0) -> None:
            self.host = host
            self.hostname = hostname
            self.port = port

    class _DhcpServiceInfo:
        """Stub for DhcpServiceInfo (only the fields the flow reads)."""

        def __init__(self, ip: str = "", hostname: str = "", macaddress: str = "") -> None:
            self.ip = ip
            self.hostname = hostname
            self.macaddress = macaddress

    ha_si_zc = _mod("homeassistant.helpers.service_info.zeroconf", ha_si)
    ha_si_zc.ZeroconfServiceInfo = _ZeroconfServiceInfo  # type: ignore[attr-defined]

    ha_si_dhcp = _mod("homeassistant.helpers.service_info.dhcp", ha_si)
    ha_si_dhcp.DhcpServiceInfo = _DhcpServiceInfo  # type: ignore[attr-defined]

    ha_rs = _mod("homeassistant.helpers.restore_state", ha_helpers)

    class _RestoreEntity:
        """Stub for RestoreEntity."""

        async def async_added_to_hass(self) -> None:
            pass

        async def async_get_last_state(self) -> None:
            return None

    ha_rs.RestoreEntity = _RestoreEntity  # type: ignore[attr-defined]

    # homeassistant.components.*
    ha_comp = _mod("homeassistant.components", ha)

    ha_vac = _mod("homeassistant.components.vacuum", ha_comp)
    class _Segment:
        """Stub for homeassistant.components.vacuum.Segment."""
        def __init__(self, *, id: str, name: str, group: str | None = None) -> None:
            self.id = id
            self.name = name
            self.group = group

    ha_vac.Segment = _Segment  # type: ignore[attr-defined]

    class _StateVacuumEntity:
        """Stub for StateVacuumEntity base class."""
        last_seen_segments: list | None = None

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_create_segments_issue(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    ha_vac.StateVacuumEntity = _StateVacuumEntity  # type: ignore[attr-defined]

    class _VacuumActivity:
        """Stub for VacuumActivity enum."""
        IDLE = "idle"
        CLEANING = "cleaning"
        DOCKED = "docked"
        PAUSED = "paused"
        RETURNING = "returning"
        ERROR = "error"

    ha_vac.VacuumActivity = _VacuumActivity  # type: ignore[attr-defined]

    class _VacuumEntityFeature:
        """Stub for VacuumEntityFeature flags."""
        STATE = 1
        START = 2
        STOP = 4
        PAUSE = 8
        RETURN_HOME = 16
        FAN_SPEED = 32
        LOCATE = 64
        CLEAN_AREA = 128

        def __or__(self, other: object) -> int:
            return 0

        def __ror__(self, other: object) -> int:
            return 0

    ha_vac.VacuumEntityFeature = _VacuumEntityFeature  # type: ignore[attr-defined]

    ha_select = _mod("homeassistant.components.select", ha_comp)

    @dataclass(frozen=True, kw_only=True)
    class _SelectEntityDescription:
        """Stub for SelectEntityDescription (the EntityDescription fields our code sets)."""

        key: str
        name: str | None = None
        translation_key: str | None = None
        entity_category: object | None = None
        options: list | None = None

    ha_select.SelectEntityDescription = _SelectEntityDescription  # type: ignore[attr-defined]

    class _SelectEntity:
        """Stub for SelectEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    ha_select.SelectEntity = _SelectEntity  # type: ignore[attr-defined]

    ha_button = _mod("homeassistant.components.button", ha_comp)

    class _ButtonEntity:
        """Stub for ButtonEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    @dataclass(frozen=True, kw_only=True)
    class _ButtonEntityDescription:
        """Stub for ButtonEntityDescription."""

        key: str
        name: str | None = None
        translation_key: str | None = None
        entity_category: object | None = None

    ha_button.ButtonEntity = _ButtonEntity  # type: ignore[attr-defined]
    ha_button.ButtonEntityDescription = _ButtonEntityDescription  # type: ignore[attr-defined]

    ha_number = _mod("homeassistant.components.number", ha_comp)

    class _NumberMode:
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    ha_number.NumberMode = _NumberMode  # type: ignore[attr-defined]

    class _RestoreNumber:
        """Stub for RestoreNumber base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        async def async_added_to_hass(self) -> None:
            pass

        async def async_get_last_number_data(self) -> None:
            return None

        def async_write_ha_state(self) -> None:
            pass

    ha_number.RestoreNumber = _RestoreNumber  # type: ignore[attr-defined]

    ha_sensor = _mod("homeassistant.components.sensor", ha_comp)

    class _SensorEntity:
        """Stub for SensorEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    ha_sensor.SensorEntity = _SensorEntity  # type: ignore[attr-defined]

    class _SensorDeviceClass:
        BATTERY = "battery"
        DURATION = "duration"
        ENUM = "enum"

    class _SensorStateClass:
        MEASUREMENT = "measurement"

    ha_sensor.SensorDeviceClass = _SensorDeviceClass  # type: ignore[attr-defined]
    ha_sensor.SensorStateClass = _SensorStateClass  # type: ignore[attr-defined]

    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        """Stub for SensorEntityDescription fields used by Narwal sensors."""

        key: str
        name: str | None = None
        translation_key: str | None = None
        entity_category: object | None = None
        device_class: object | None = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        state_class: object | None = None
        options: list | None = None

    ha_sensor.SensorEntityDescription = _SensorEntityDescription  # type: ignore[attr-defined]

    ha_bs = _mod("homeassistant.components.binary_sensor", ha_comp)

    class _BinarySensorEntity:
        """Stub for BinarySensorEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

    ha_bs.BinarySensorEntity = _BinarySensorEntity  # type: ignore[attr-defined]
    ha_bs.BinarySensorDeviceClass = MagicMock()  # type: ignore[attr-defined]

    @dataclass(frozen=True, kw_only=True)
    class _BinarySensorEntityDescription:
        """Stub for BinarySensorEntityDescription (fields our code sets)."""

        key: str
        name: str | None = None
        translation_key: str | None = None
        entity_category: object | None = None
        device_class: object | None = None

    ha_bs.BinarySensorEntityDescription = _BinarySensorEntityDescription  # type: ignore[attr-defined]

    ha_switch = _mod("homeassistant.components.switch", ha_comp)

    class _SwitchEntity:
        """Stub for SwitchEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    @dataclass(frozen=True, kw_only=True)
    class _SwitchEntityDescription:
        """Stub for SwitchEntityDescription."""

        key: str
        name: str | None = None
        translation_key: str | None = None
        entity_category: object | None = None

    ha_switch.SwitchEntity = _SwitchEntity  # type: ignore[attr-defined]
    ha_switch.SwitchEntityDescription = _SwitchEntityDescription  # type: ignore[attr-defined]

    ha_cam = _mod("homeassistant.components.camera", ha_comp)

    class _Camera:
        """Stub for Camera base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def __init__(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    ha_cam.Camera = _Camera  # type: ignore[attr-defined]

    ha_light = _mod("homeassistant.components.light", ha_comp)
    ha_light.ATTR_EFFECT = "effect"  # type: ignore[attr-defined]
    ha_light.DOMAIN = "light"  # type: ignore[attr-defined]

    class _LightEntity:
        """Stub for LightEntity base class."""

        def __init_subclass__(cls, **kw: object) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    class _ColorMode:
        ONOFF = "onoff"

    class _LightEntityFeature:
        EFFECT = 1

    ha_light.LightEntity = _LightEntity  # type: ignore[attr-defined]
    ha_light.ColorMode = _ColorMode  # type: ignore[attr-defined]
    ha_light.LightEntityFeature = _LightEntityFeature  # type: ignore[attr-defined]
