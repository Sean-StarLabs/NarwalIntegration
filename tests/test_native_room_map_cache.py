"""Native room cleaning reuses a session map on non-broadcasting robots."""

from unittest.mock import AsyncMock

import pytest

from tests.test_vacuum_non_broadcast import _make_cx7
from tests.test_vacuum_segments import Segment
from homeassistant.exceptions import HomeAssistantError
from narwal_client.models import MapData, RoomInfo


async def test_known_cx7_rooms_dispatch_without_a_map_round_trip():
    vacuum = _make_cx7()
    client = vacuum.coordinator.client
    client.get_map.side_effect = TimeoutError("no map reply")

    await vacuum.async_clean_segments(["1", "3", "3"])

    client.get_map.assert_not_awaited()
    client.start_rooms.assert_awaited_once()
    assert client.start_rooms.await_args.args[0] == [1, 3]
    vacuum.coordinator.async_prepare_clean_start.assert_awaited_once()


@pytest.mark.parametrize("missing", ["map", "map_id", "room"])
async def test_incomplete_cache_still_refreshes_and_surfaces_failure(missing):
    vacuum = _make_cx7()
    client = vacuum.coordinator.client
    if missing == "map":
        client.state.map_data = None
        # Restored HA segments alone are insufficient for dispatch.
        vacuum.last_seen_segments = [Segment(id="2", name="Kitchen")]
    elif missing == "map_id":
        client.state.map_data.map_id = 0
    else:
        client.state.map_data.rooms = [RoomInfo(room_id=1)]
    client.get_map.side_effect = TimeoutError("no map reply")

    with pytest.raises(HomeAssistantError, match="map could not be refreshed"):
        await vacuum.async_clean_segments(["2"])

    client.get_map.assert_awaited_once()
    client.start_rooms.assert_not_awaited()


async def test_new_room_is_validated_against_the_refreshed_map():
    vacuum = _make_cx7()
    client = vacuum.coordinator.client

    async def refresh():
        client.state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=4)])

    client.get_map.side_effect = refresh
    await vacuum.async_clean_segments(["4"])

    client.get_map.assert_awaited_once()
    assert client.start_rooms.await_args.args[0] == [4]


async def test_unknown_room_after_refresh_is_rejected():
    vacuum = _make_cx7()
    client = vacuum.coordinator.client

    with pytest.raises(HomeAssistantError, match="Unknown Narwal room ID: 99"):
        await vacuum.async_clean_segments(["99"])

    client.get_map.assert_awaited_once()
    client.start_rooms.assert_not_awaited()


async def test_broadcasting_model_still_refreshes_known_rooms():
    vacuum = _make_cx7(supports_broadcasts=True)
    client = vacuum.coordinator.client
    client.get_map.side_effect = TimeoutError("no map reply")

    with pytest.raises(HomeAssistantError, match="map could not be refreshed"):
        await vacuum.async_clean_segments(["1"])

    client.get_map.assert_awaited_once()
    client.start_rooms.assert_not_awaited()


async def test_cached_map_does_not_bypass_start_preparation():
    vacuum = _make_cx7()
    client = vacuum.coordinator.client
    vacuum.coordinator.async_prepare_clean_start = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="cannot be started right now"):
        await vacuum.async_clean_segments(["1"])

    client.get_map.assert_not_awaited()
    client.start_rooms.assert_not_awaited()
