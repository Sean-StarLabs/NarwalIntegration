"""Tests for Narwal integration services."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal import (
    FIELD_LED_MODE,
    FIELD_MODE,
    FIELD_MOP_STRENGTH,
    FIELD_PASSES,
    FIELD_ROOMS,
    FIELD_SUCTION,
    FIELD_WATER,
    _async_get_service_coordinators,
    _async_register_services,
    _async_validate_clean_rooms_targets,
    async_setup,
)
from custom_components.narwal.const import (
    DOMAIN,
    SERVICE_CLEAN_ROOMS,
    SERVICE_SET_DOCK_LIGHT,
)
from custom_components.narwal.coordinator import NarwalCoordinator
from custom_components.narwal.narwal_client import CommandResult
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import service


def test_clean_rooms_awaits_entity_target_extraction() -> None:
    service.async_extract_entity_ids.reset_mock()
    hass = MagicMock()
    client = SimpleNamespace(
        robot_awake=True,
        state=MagicMock(),
        start_rooms=AsyncMock(
            return_value=SimpleNamespace(result_code=CommandResult.SUCCESS)
        ),
    )
    coordinator = SimpleNamespace(client=client, async_set_updated_data=MagicMock())
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            "entity_id": ["vacuum.flow_2"],
            FIELD_ROOMS: [1],
            FIELD_MODE: "vacuum",
            FIELD_SUCTION: "standard",
            FIELD_WATER: "normal",
            FIELD_MOP_STRENGTH: "normal",
            FIELD_PASSES: 1,
        }
    )

    _async_register_services(hass)
    handlers = {
        registration.args[1]: registration.args[2]
        for registration in hass.services.async_register.call_args_list
    }
    handler = handlers[SERVICE_CLEAN_ROOMS]

    with (
        patch(
            "custom_components.narwal._async_get_service_coordinators",
            new=AsyncMock(return_value=[coordinator]),
        ),
        patch(
            "custom_components.narwal._async_room_ids_for_coordinator",
            new=AsyncMock(return_value=[1]),
        ),
        patch(
            "custom_components.narwal._async_validate_clean_rooms_targets",
            new=AsyncMock(return_value=["vacuum.flow_2"]),
        ),
    ):
        asyncio.run(handler(call))

    service.async_extract_entity_ids.assert_awaited_once_with(call)
    client.start_rooms.assert_awaited_once()
    hass.services.async_register.assert_any_call(
        DOMAIN,
        SERVICE_CLEAN_ROOMS,
        handler,
        schema=ANY,
    )


async def test_services_are_registered_during_integration_setup() -> None:
    hass = MagicMock()

    assert await async_setup(hass, {}) is True

    assert hass.services.async_register.call_count == 3


async def test_clean_rooms_requires_an_explicit_target() -> None:
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": NarwalCoordinator.__new__(NarwalCoordinator)}}

    with pytest.raises(HomeAssistantError, match="Target a Narwal vacuum"):
        await _async_get_service_coordinators(hass, [])


async def test_clean_rooms_rejects_unauthorized_target() -> None:
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=DOMAIN)
    user = SimpleNamespace(
        is_admin=False,
        permissions=SimpleNamespace(check_entity=MagicMock(return_value=False)),
    )
    hass.auth.async_get_user = AsyncMock(return_value=user)
    call = SimpleNamespace(
        context=SimpleNamespace(user_id="restricted-user"),
        data={"entity_id": ["vacuum.flow_2"]},
    )

    with (
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Unauthorized),
    ):
        await _async_validate_clean_rooms_targets(
            hass, call, ["vacuum.flow_2"]
        )

    user.permissions.check_entity.assert_called_once_with(
        "vacuum.flow_2", "control"
    )


async def test_clean_rooms_rejects_non_vacuum_target() -> None:
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=DOMAIN)
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={"entity_id": ["sensor.flow_2_battery"]},
    )

    with (
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="Narwal vacuum entity"),
    ):
        await _async_validate_clean_rooms_targets(
            hass, call, ["sensor.flow_2_battery"]
        )


async def test_clean_rooms_filters_indirect_non_vacuum_targets() -> None:
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.side_effect = {
        "vacuum.flow_2": SimpleNamespace(platform=DOMAIN),
        "sensor.flow_2_battery": SimpleNamespace(platform=DOMAIN),
    }.get
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={"device_id": ["flow-2-device"]},
    )

    with patch("custom_components.narwal.er.async_get", return_value=registry):
        entity_ids = await _async_validate_clean_rooms_targets(
            hass,
            call,
            ["sensor.flow_2_battery", "vacuum.flow_2"],
        )

    assert entity_ids == ["vacuum.flow_2"]


@pytest.mark.parametrize("direct_target", ["all", "group.vacuums"])
async def test_clean_rooms_accepts_expanded_targets(direct_target: str) -> None:
    """Expanded all and group targets validate their resolved Narwal entities."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=DOMAIN)
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={"entity_id": [direct_target]},
    )

    with patch("custom_components.narwal.er.async_get", return_value=registry):
        entity_ids = await _async_validate_clean_rooms_targets(
            hass,
            call,
            ["vacuum.flow_2"],
        )

    assert entity_ids == ["vacuum.flow_2"]


def test_set_dock_light_awaits_entity_target_extraction() -> None:
    service.async_extract_entity_ids.reset_mock()
    hass = MagicMock()
    client = SimpleNamespace(
        robot_awake=True,
        set_ambient_light_mode=AsyncMock(
            return_value=SimpleNamespace(result_code=CommandResult.APPLIED)
        ),
    )
    coordinator = SimpleNamespace(
        client=client,
        config_entry=SimpleNamespace(
            data={"product_key": "QxMSPG6VSO"},
            options={},
            title="Flow 2",
        ),
        async_request_refresh=AsyncMock(),
    )
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            "entity_id": ["vacuum.flow_2"],
            FIELD_LED_MODE: "nightlight",
        }
    )

    _async_register_services(hass)
    handlers = {
        registration.args[1]: registration.args[2]
        for registration in hass.services.async_register.call_args_list
    }
    handler = handlers[SERVICE_SET_DOCK_LIGHT]

    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=DOMAIN)
    with (
        patch("custom_components.narwal.er.async_get", return_value=registry),
        patch(
            "custom_components.narwal._async_get_service_coordinators",
            new=AsyncMock(return_value=[coordinator]),
        ),
    ):
        asyncio.run(handler(call))

    service.async_extract_entity_ids.assert_awaited_once_with(call)
    client.set_ambient_light_mode.assert_awaited_once()
    coordinator.async_request_refresh.assert_awaited_once()


def test_set_dock_light_rejects_empty_explicit_target() -> None:
    service.async_extract_entity_ids.reset_mock()
    service.async_extract_entity_ids.return_value = set()
    hass = MagicMock()
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            "area_id": ["bedroom"],
            FIELD_LED_MODE: "nightlight",
        }
    )

    _async_register_services(hass)
    handlers = {
        registration.args[1]: registration.args[2]
        for registration in hass.services.async_register.call_args_list
    }

    with pytest.raises(HomeAssistantError, match="Target does not contain"):
        asyncio.run(handlers[SERVICE_SET_DOCK_LIGHT](call))

    service.async_extract_entity_ids.assert_awaited_once_with(call)


def test_set_dock_light_rejects_unauthorized_target() -> None:
    service.async_extract_entity_ids.reset_mock()
    hass = MagicMock()
    user = SimpleNamespace(
        is_admin=False,
        permissions=SimpleNamespace(check_entity=MagicMock(return_value=False)),
    )
    hass.auth.async_get_user = AsyncMock(return_value=user)
    call = SimpleNamespace(
        context=SimpleNamespace(user_id="restricted-user"),
        data={
            "entity_id": ["vacuum.flow_2"],
            FIELD_LED_MODE: "nightlight",
        },
    )

    _async_register_services(hass)
    handlers = {
        registration.args[1]: registration.args[2]
        for registration in hass.services.async_register.call_args_list
    }

    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=DOMAIN)
    with (
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Unauthorized),
    ):
        asyncio.run(handlers[SERVICE_SET_DOCK_LIGHT](call))

    user.permissions.check_entity.assert_called_once_with(
        "vacuum.flow_2", "control"
    )


def test_set_dock_light_filters_indirect_non_vacuum_targets() -> None:
    hass = MagicMock()
    coordinator = SimpleNamespace(
        client=SimpleNamespace(
            robot_awake=True,
            set_ambient_light_mode=AsyncMock(
                return_value=SimpleNamespace(result_code=CommandResult.APPLIED)
            ),
        ),
        config_entry=SimpleNamespace(
            data={"product_key": "QxMSPG6VSO"},
            options={},
            title="Flow 2",
        ),
        async_request_refresh=AsyncMock(),
    )
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={"device_id": ["flow-2-device"], FIELD_LED_MODE: "nightlight"},
    )
    registry = MagicMock()
    registry.async_get.side_effect = {
        "vacuum.flow_2": SimpleNamespace(platform=DOMAIN),
        "sensor.flow_2_battery": SimpleNamespace(platform=DOMAIN),
    }.get

    _async_register_services(hass)
    handlers = {
        registration.args[1]: registration.args[2]
        for registration in hass.services.async_register.call_args_list
    }
    extract_targets = AsyncMock(
        return_value={"sensor.flow_2_battery", "vacuum.flow_2"}
    )
    get_coordinators = AsyncMock(return_value=[coordinator])

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new=extract_targets,
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        patch(
            "custom_components.narwal._async_get_service_coordinators",
            new=get_coordinators,
        ),
    ):
        asyncio.run(handlers[SERVICE_SET_DOCK_LIGHT](call))

    get_coordinators.assert_awaited_once_with(hass, ["vacuum.flow_2"])
