"""Tests for vacuum entity Segment API (room-specific cleaning).

Tests async_get_segments, async_clean_segments, and _check_segment_changes
on the NarwalVacuum entity using HA stubs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.coordinator import CleanSettings  # noqa: E402
from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402
from narwal_client.const import CommandResult, WorkingStatus  # noqa: E402
from narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_MOP,
    CommandResponse,
    MapData,
    NarwalState,
    RoomInfo,
)

Segment = sys.modules["homeassistant.components.vacuum"].Segment


def _make_vacuum(state: NarwalState | None = None) -> NarwalVacuum:
    """Create a NarwalVacuum with mocked coordinator."""
    client_state = state or NarwalState()
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = client_state
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.clean_settings = CleanSettings()
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
    coordinator.dock_action_lock = asyncio.Lock()

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}

    # Stub StateVacuumEntity attributes
    vac.last_seen_segments = None
    vac.async_create_segments_issue = MagicMock()
    vac.async_write_ha_state = MagicMock()

    return vac


def _docked_state() -> NarwalState:
    """Return a state whose reported status is idle on the dock."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    return state


class TestAsyncGetSegments:
    """Tests for async_get_segments."""

    async def test_no_state_no_cache_returns_empty(self) -> None:
        """Returns [] when coordinator.data is None and no cached segments."""
        vac = _make_vacuum(state=None)
        result = await vac.async_get_segments()
        assert result == []

    async def test_no_map_data_no_cache_returns_empty(self) -> None:
        """Returns [] when state.map_data is None and no cached segments."""
        state = NarwalState()
        state.map_data = None
        vac = _make_vacuum(state=state)
        result = await vac.async_get_segments()
        assert result == []

    async def test_no_state_returns_cached_segments(self) -> None:
        """Falls back to last_seen_segments when coordinator.data is None."""
        vac = _make_vacuum(state=None)
        cached = [Segment(id="7", name="Lavanderia", group="Rooms")]
        vac.last_seen_segments = cached
        result = await vac.async_get_segments()
        assert len(result) == 1
        assert result[0].id == "7"
        assert result[0].name == "Lavanderia"

    async def test_no_map_data_returns_cached_segments(self) -> None:
        """Falls back to last_seen_segments when map_data is None (robot sleeping)."""
        state = NarwalState()
        state.map_data = None
        vac = _make_vacuum(state=state)
        cached = [
            Segment(id="1", name="Living Room", group="Rooms"),
            Segment(id="2", name="Kitchen", group="Rooms"),
        ]
        vac.last_seen_segments = cached
        result = await vac.async_get_segments()
        assert len(result) == 2
        ids = [s.id for s in result]
        assert "1" in ids
        assert "2" in ids

    async def test_returns_segments_from_rooms(self) -> None:
        """Returns Segment objects for each room with room_id > 0."""
        rooms = [
            RoomInfo(room_id=0, name="Unknown", room_sub_type=0, category=1),
            RoomInfo(room_id=11, name="Pantry", room_sub_type=10, category=2),
            RoomInfo(room_id=9, name="Kitchen", room_sub_type=4, category=1),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms)
        vac = _make_vacuum(state=state)

        result = await vac.async_get_segments()

        assert len(result) == 2, "room_id=0 should be filtered out"
        ids = [s.id for s in result]
        assert "11" in ids
        assert "9" in ids
        # IDs are strings
        for seg in result:
            assert isinstance(seg.id, str)

    async def test_segment_names_match_display_name(self) -> None:
        """Segment.name comes from RoomInfo.display_name."""
        rooms = [
            RoomInfo(room_id=1, name="Master Suite", room_sub_type=1, category=1),
            RoomInfo(room_id=2, name="", room_sub_type=6, category=1, instance_index=2),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms)
        vac = _make_vacuum(state=state)

        result = await vac.async_get_segments()

        names = {s.id: s.name for s in result}
        assert names["1"] == "Master Suite"
        assert names["2"] == "Toilet 2"

    async def test_segment_groups_by_category(self) -> None:
        """Category 1 -> group='Rooms', category 2 -> group='Utility'."""
        rooms = [
            RoomInfo(room_id=1, name="Living Room", room_sub_type=3, category=1),
            RoomInfo(room_id=2, name="Pantry", room_sub_type=10, category=2),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms)
        vac = _make_vacuum(state=state)

        result = await vac.async_get_segments()

        groups = {s.id: s.group for s in result}
        assert groups["1"] == "Rooms"
        assert groups["2"] == "Utility"

    async def test_skips_room_id_zero(self) -> None:
        """Rooms with room_id=0 are filtered out."""
        rooms = [
            RoomInfo(room_id=0, name="", room_sub_type=0, category=0),
            RoomInfo(room_id=5, name="Study", room_sub_type=5, category=1),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms)
        vac = _make_vacuum(state=state)

        result = await vac.async_get_segments()
        assert len(result) == 1
        assert result[0].id == "5"


class TestAsyncCleanSegments:
    """Tests for async_clean_segments."""

    async def test_converts_string_ids_and_calls_start_rooms(self) -> None:
        """Converts string segment IDs to int and calls start_rooms with the settings."""
        state = _docked_state()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11), RoomInfo(room_id=9)])
        vac = _make_vacuum(state=state)
        settings = vac.coordinator.clean_settings
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        # Mock wake so it's a no-op
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.wake = AsyncMock()

        await vac.async_clean_segments(["11", "9"])

        vac.coordinator.client.start_rooms.assert_awaited_once_with(
            [11, 9],
            work_mode=settings.work_mode,
            fan=settings.fan,
            water=settings.water,
            mop_strength=settings.mop_strength,
            passes=settings.passes,
        )

    async def test_segment_clean_accepted_response_does_not_warn(self, caplog) -> None:
        """Accepted async start responses are not room-clean failures."""
        state = _docked_state()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        with caplog.at_level(logging.WARNING, logger="custom_components.narwal.vacuum"):
            await vac.async_clean_segments(["11"])

        assert "Room clean failed" not in caplog.text

    async def test_rejects_room_clean_when_dock_task_is_active(self) -> None:
        """Room clean requests are blocked before dispatch during dock work."""
        state = _docked_state()
        state.station_activity = 1
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError):
            await vac.async_clean_segments(["11"])

        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_accepted_room_clean_reserves_robot_start_context(self) -> None:
        """Accepted room-start commands block immediate dock starts."""
        state = _docked_state()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        await vac.async_clean_segments(["11"])

        assert state.has_assumed_robot_clean

    async def test_rejected_room_clean_raises_service_error(self) -> None:
        """Rejected clean/start_clean responses fail the HA service call."""
        state = _docked_state()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        )

        with pytest.raises(HomeAssistantError, match="Narwal room clean failed"):
            await vac.async_clean_segments(["11"])

    async def test_room_clean_waits_for_dock_action_lock(self) -> None:
        """Robot starts cannot validate against the same idle snapshot as dock tasks."""
        state = _docked_state()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
        )
        await vac.coordinator.dock_action_lock.acquire()

        task = asyncio.create_task(vac.async_clean_segments(["11"]))
        await asyncio.sleep(0)

        vac.coordinator.client.start_rooms.assert_not_awaited()
        vac.coordinator.dock_action_lock.release()
        await task
        vac.coordinator.client.start_rooms.assert_awaited_once()

    async def test_non_numeric_segment_id_raises(self) -> None:
        state = _docked_state()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(Exception, match="numeric"):
            await vac.async_clean_segments(["kitchen"])

        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_unknown_segment_id_raises(self) -> None:
        state = _docked_state()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(Exception, match="Unknown Narwal room ID"):
            await vac.async_clean_segments(["99"])

        vac.coordinator.client.start_rooms.assert_not_awaited()


class TestCheckSegmentChanges:
    """Tests for _check_segment_changes."""

    def test_no_last_seen_does_nothing(self) -> None:
        """When last_seen_segments is None, does nothing."""
        state = NarwalState()
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = None

        vac._check_segment_changes()

        vac.async_create_segments_issue.assert_not_called()

    def test_detects_room_changes(self) -> None:
        """Calls async_create_segments_issue when rooms differ."""
        rooms_old = [
            Segment(id="1", name="Kitchen"),
            Segment(id="2", name="Bathroom"),
        ]
        rooms_new = [
            RoomInfo(room_id=1, name="Kitchen", room_sub_type=4, category=1),
            RoomInfo(room_id=3, name="Study", room_sub_type=5, category=1),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms_new)
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = rooms_old

        vac._check_segment_changes()

        vac.async_create_segments_issue.assert_called_once()

    def test_no_change_when_same_rooms(self) -> None:
        """Does NOT call async_create_segments_issue when rooms match."""
        rooms_old = [
            Segment(id="1", name="Kitchen"),
            Segment(id="2", name="Bathroom"),
        ]
        rooms_new = [
            RoomInfo(room_id=1, name="Kitchen", room_sub_type=4, category=1),
            RoomInfo(room_id=2, name="Bathroom", room_sub_type=6, category=1),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=rooms_new)
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = rooms_old

        vac._check_segment_changes()

        vac.async_create_segments_issue.assert_not_called()

    def test_no_map_data_does_nothing(self) -> None:
        """When map_data is None but last_seen_segments exists, does nothing."""
        state = NarwalState()
        state.map_data = None
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = [Segment(id="1", name="Kitchen")]

        vac._check_segment_changes()

        vac.async_create_segments_issue.assert_not_called()


class TestAsyncStartWholeHouse:
    """async_start runs a whole-house clean via start_rooms(all rooms), not the saved plan."""

    async def test_enumerates_all_rooms(self, caplog) -> None:
        """Whole-house start passes every room id to clean/start_clean, skipping plan/start."""
        state = NarwalState()
        state.map_data = MapData(map_id=2, rooms=[
            RoomInfo(room_id=1), RoomInfo(room_id=2), RoomInfo(room_id=0),  # 0 filtered
        ])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        vac.coordinator.client.start = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="custom_components.narwal.vacuum"):
            await vac.async_start()

        vac.coordinator.client.start_rooms.assert_awaited_once()
        assert vac.coordinator.client.start_rooms.await_args.args[0] == [1, 2]
        vac.coordinator.client.start.assert_not_called()
        assert "Start command was rejected" not in caplog.text
        assert state.has_assumed_robot_clean

    async def test_falls_back_to_saved_plan_without_map(self) -> None:
        """With no map rooms available, falls back to the saved-plan start()."""
        state = NarwalState()  # no map_data
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.get_map = AsyncMock()  # does not populate map_data
        vac.coordinator.client.start = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        await vac.async_start()

        vac.coordinator.client.start.assert_awaited_once()
        vac.coordinator.client.start_rooms.assert_not_called()

    async def test_rejected_whole_house_start_raises_service_error(self) -> None:
        """Rejected whole-house starts fail the HA service call."""
        state = NarwalState()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=1)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        )

        with pytest.raises(HomeAssistantError, match="Narwal start command failed"):
            await vac.async_start()


class TestAsyncStop:
    """Tests for stop routing between robot and dock task contexts."""

    async def test_stop_routes_dock_only_task_through_dock_policy(self) -> None:
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_presence = 6
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        await vac.async_stop()

        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_awaited_once_with()

    async def test_stop_rejects_ambiguous_dock_only_task(self) -> None:
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_presence = 6
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock()

        with pytest.raises(HomeAssistantError, match="cannot be stopped safely"):
            await vac.async_stop()

        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()
