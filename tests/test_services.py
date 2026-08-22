"""Tests for Narwal domain services."""

from __future__ import annotations

from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal import (  # noqa: E402
    FIELD_MODE,
    FIELD_MOP_STRENGTH,
    FIELD_PASSES,
    FIELD_ROOMS,
    FIELD_ROUTE,
    FIELD_SUCTION,
    FIELD_WATER,
    _async_register_services,
)
from custom_components.narwal.const import DOMAIN, SERVICE_CLEAN_ROOMS  # noqa: E402
from custom_components.narwal.coordinator import CleanSettings, NarwalCoordinator  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MapData,
    NarwalState,
    RoomInfo,
    WorkingStatus,
)


def _coordinator(
    *,
    docked: bool = True,
    result_code: int = CommandResult.SUCCESS,
    product_key: str = "QoEsI5qYXO",
) -> NarwalCoordinator:
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState()
    if docked:
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
    state.map_data = MapData(rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)])
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.client.state = state
    coordinator.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=result_code)
    )
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"product_key": product_key}
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.active_room_ids = None
    coordinator.data = state
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _register_clean_rooms_handler(coordinator: NarwalCoordinator):
    """Register the test service handler for a coordinator."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    hass.services.has_service.return_value = False
    handlers = {}

    def register_service(domain: str, service_name: str, handler, **kwargs) -> None:
        handlers[(domain, service_name)] = handler

    hass.services.async_register.side_effect = register_service
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry-1",
        platform=DOMAIN,
    )
    _async_register_services(hass)
    return handlers[(DOMAIN, SERVICE_CLEAN_ROOMS)], registry


def _clean_rooms_call() -> SimpleNamespace:
    """Return a service call with non-default settings."""
    return SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            FIELD_ROOMS: [4, 7],
            FIELD_MODE: "vacuum_and_mop",
            FIELD_SUCTION: "strong",
            FIELD_WATER: "wet",
            FIELD_MOP_STRENGTH: "high",
            FIELD_PASSES: 2,
            FIELD_ROUTE: "standard",
        },
    )


async def test_clean_rooms_service_records_active_room_ids() -> None:
    """The service path stores the requested room plan after a successful start."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ) as extract_entity_ids,
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    extract_entity_ids.assert_awaited_once()
    assert extract_entity_ids.await_args.args[1] is call
    coordinator.client.start_rooms.assert_awaited_once()
    assert coordinator.client.start_rooms.await_args.args[0] == [4, 7]
    assert coordinator.active_room_ids == [4, 7]
    coordinator.async_set_updated_data.assert_called_once_with(coordinator.client.state)


async def test_clean_rooms_service_uses_requested_settings_over_room_profiles() -> None:
    """Explicit service settings should not be silently overridden by room profiles."""
    coordinator = _coordinator()
    coordinator.set_room_clean_setting(4, "fan", FanLevel.MUTE)
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    kwargs = coordinator.client.start_rooms.await_args.kwargs
    assert kwargs["room_settings"][4].fan == FanLevel.STRONG
    assert kwargs["room_settings"][7].fan == FanLevel.STRONG


async def test_clean_rooms_service_rejects_unavailable_start() -> None:
    """The service enforces the same availability as the start entities."""
    coordinator = _coordinator(docked=False)
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="cannot be started"),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_wakes_unknown_before_starting() -> None:
    """An initially unknown sleeping robot can wake before strict start validation."""
    coordinator = _coordinator(docked=False)
    coordinator.client.robot_awake = False

    async def wake_robot(*, timeout: float) -> bool:
        coordinator.client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
        return True

    coordinator.client.wake = AsyncMock(side_effect=wake_robot)
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
    coordinator.client.start_rooms.assert_awaited_once()


async def test_clean_rooms_service_revalidates_after_room_resolution() -> None:
    """Map lookup can refresh state; do not send a clean if the robot became busy."""
    coordinator = _coordinator()
    coordinator.client.state.map_data = None

    async def refresh_map() -> None:
        coordinator.client.state.map_data = MapData(
            rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)]
        )
        coordinator.client.state.working_status = WorkingStatus.CLEANING_ALT
        coordinator.client.state.last_active_working_status_time = time.monotonic()

    coordinator.client.get_map = AsyncMock(side_effect=refresh_map)
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="cannot be started"),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_preserves_settings_on_rejected_start() -> None:
    """Rejected starts must not alter the pending settings for the next clean."""
    coordinator = _coordinator(result_code=CommandResult.CONFLICT)
    handler, registry = _register_clean_rooms_handler(coordinator)
    original_settings = CleanSettings(**coordinator.clean_settings.__dict__)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="failed"),
    ):
        await handler(_clean_rooms_call())

    assert coordinator.clean_settings == original_settings
    assert coordinator.active_room_ids is None


async def test_clean_rooms_service_rejects_unsupported_ultra_suction() -> None:
    """AX26 must not silently map unsupported Ultra suction to Strong."""
    coordinator = _coordinator(product_key="qV6BujoYLz")
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_SUCTION] = "ultra"

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="Ultra suction is not supported"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_rejects_invalid_room_ids() -> None:
    """Room IDs must be valid positive integers."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_ROOMS] = [4, 0]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="positive"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_rejects_unknown_room_ids() -> None:
    """Room IDs must exist on the active map."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_ROOMS] = [4, 99]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="Unknown Narwal room ID"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_preflights_all_targets_before_starting() -> None:
    """A later invalid target must not leave an earlier vacuum already running."""
    first = _coordinator()
    first.client.robot_awake = False
    first.client.wake = AsyncMock()
    second = _coordinator()
    second.client.state.map_data = MapData(rooms=[RoomInfo(room_id=99)])
    second.data = second.client.state

    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-1": first, "entry-2": second}}
    hass.services.has_service.return_value = False
    handlers = {}

    def register_service(domain: str, service_name: str, handler, **kwargs) -> None:
        handlers[(domain, service_name)] = handler

    hass.services.async_register.side_effect = register_service
    registry = MagicMock()
    registry.async_get.side_effect = lambda entity_id: {
        "vacuum.first_narwal": SimpleNamespace(
            config_entry_id="entry-1",
            platform=DOMAIN,
        ),
        "vacuum.second_narwal": SimpleNamespace(
            config_entry_id="entry-2",
            platform=DOMAIN,
        ),
    }[entity_id]
    _async_register_services(hass)
    handler = handlers[(DOMAIN, SERVICE_CLEAN_ROOMS)]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.first_narwal", "vacuum.second_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="Unknown Narwal room ID"),
    ):
        await handler(_clean_rooms_call())

    first.client.wake.assert_not_awaited()
    first.client.start_rooms.assert_not_awaited()
    second.client.start_rooms.assert_not_awaited()
