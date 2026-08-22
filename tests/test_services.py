"""Tests for Narwal domain services."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    NarwalState,
)


def _coordinator() -> NarwalCoordinator:
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.client.state = NarwalState()
    coordinator.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.clean_settings = CleanSettings()
    coordinator.active_room_ids = None
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


async def test_clean_rooms_service_records_active_room_ids() -> None:
    """The service path stores the requested room plan after a successful start."""
    coordinator = _coordinator()
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
    call = SimpleNamespace(
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

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        _async_register_services(hass)
        await handlers[(DOMAIN, SERVICE_CLEAN_ROOMS)](call)

    coordinator.client.start_rooms.assert_awaited_once()
    assert coordinator.client.start_rooms.await_args.args[0] == [4, 7]
    assert coordinator.active_room_ids == [4, 7]
    coordinator.async_set_updated_data.assert_called_once_with(coordinator.client.state)
