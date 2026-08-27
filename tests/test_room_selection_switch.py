"""Tests for Narwal room-selection switches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.switch import SwitchEntity  # noqa: E402
from homeassistant.helpers.restore_state import RestoreEntity  # noqa: E402

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.switch import (  # noqa: E402
    NarwalRoomSelectionSwitch,
    async_setup_entry,
)
from narwal_client import NarwalState  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402


def _state(
    working_status: WorkingStatus = WorkingStatus.DOCKED,
) -> NarwalState:
    """Return a Narwal state with one room map."""
    state = NarwalState(working_status=working_status)
    state.map_data = MapData(
        map_id=100,
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
    )
    return state


def _coordinator(state: NarwalState | None = None) -> NarwalCoordinator:
    """Create a coordinator stub with real room-selection methods."""
    state = state or _state()
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.selected_clean_rooms = {}
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_add_listener = MagicMock()
    return coordinator


def test_room_selection_switch_bases() -> None:
    """Room selection switches restore HA state."""
    assert issubclass(NarwalRoomSelectionSwitch, RestoreEntity)
    assert issubclass(NarwalRoomSelectionSwitch, SwitchEntity)


async def test_room_selection_switch_updates_selected_rooms() -> None:
    """Turning the switch on/off mutates the coordinator selection state."""
    coordinator = _coordinator()
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert not switch.is_on

    await switch.async_turn_on()

    assert switch.is_on
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4]

    await switch.async_turn_off()

    assert not switch.is_on
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4, 5]
    assert coordinator.async_update_listeners.call_count == 2


async def test_room_selection_switch_restores_state() -> None:
    """Room selections persist through HA restarts."""
    coordinator = _coordinator()
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    with patch.object(
        switch,
        "async_get_last_state",
        AsyncMock(return_value=MagicMock(state="on")),
    ):
        await switch.async_added_to_hass()

    assert switch.is_on


def test_room_selection_switch_unavailable_during_active_clean() -> None:
    """Room selection is locked while clean setup cannot be edited."""
    coordinator = _coordinator(_state(WorkingStatus.CLEANING))
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert not switch.available


async def test_room_selection_entities_update_name_after_map_rename() -> None:
    """Dynamic room selection switches follow map room renames."""
    coordinator = _coordinator()
    entry = MagicMock()
    entry.runtime_data = coordinator
    added_entities = []
    listeners = []

    def add_entities(entities) -> None:
        added_entities.extend(list(entities))

    coordinator.async_add_listener.side_effect = lambda listener: listeners.append(
        listener
    )

    await async_setup_entry(MagicMock(), entry, add_entities)

    room_switch = next(
        entity
        for entity in added_entities
        if isinstance(entity, NarwalRoomSelectionSwitch)
    )
    assert room_switch._attr_name == "Kitchen selected"
    assert room_switch.extra_state_attributes["room_name"] == "Kitchen"

    coordinator.client.state.map_data = MapData(
        map_id=100,
        rooms=[RoomInfo(room_id=4, name="Pantry")],
    )
    listeners[0]()

    assert room_switch._attr_name == "Pantry selected"
    assert room_switch.extra_state_attributes["room_name"] == "Pantry"
