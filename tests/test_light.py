"""Tests for Narwal light entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.light import NarwalDockLight  # noqa: E402
from custom_components.narwal.narwal_client import CommandResult  # noqa: E402
from custom_components.narwal.narwal_client.models import CommandResponse  # noqa: E402


def _coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_device"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client.state.firmware_version = "test"
    return coordinator


def test_dock_light_preserves_unknown_state() -> None:
    """An unread dock-light mode must not be presented as off."""
    coordinator = _coordinator()
    coordinator.data.dock_light_mode = None

    dock_light = NarwalDockLight(coordinator)

    assert dock_light.is_on is None


async def test_dock_light_accepts_applied_result_code() -> None:
    """Result code 6 is accepted when the dock light visibly applies the command."""
    coordinator = _coordinator()
    coordinator.client.robot_awake = True
    coordinator.client.set_ambient_light_mode = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.APPLIED)
    )
    coordinator.async_request_refresh = AsyncMock()

    dock_light = NarwalDockLight(coordinator)
    await dock_light._set_mode("Nightlight")

    coordinator.client.set_ambient_light_mode.assert_awaited_once()
    coordinator.async_request_refresh.assert_awaited_once()
