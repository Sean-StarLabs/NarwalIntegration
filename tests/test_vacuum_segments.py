"""Tests for vacuum entity Segment API (room-specific cleaning).

Tests async_get_segments, async_clean_segments, and _check_segment_changes
on the NarwalVacuum entity using HA stubs.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from narwal_client import RoomCleanSettings  # noqa: E402
from narwal_client.const import FanLevel, WorkMode, WorkingStatus  # noqa: E402
from narwal_client.models import MapData, NarwalState, RoomInfo  # noqa: E402
from custom_components.narwal.coordinator import CleanSettings  # noqa: E402
from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402

# Grab Segment class from stubs for assertions
import sys

Segment = sys.modules["homeassistant.components.vacuum"].Segment
VacuumActivity = sys.modules["homeassistant.components.vacuum"].VacuumActivity


def _make_vacuum(state: NarwalState | None = None) -> NarwalVacuum:
    """Create a NarwalVacuum with mocked coordinator."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state or MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.clean_settings = CleanSettings()
    coordinator.active_room_ids = None
    coordinator.room_clean_settings_for_rooms = MagicMock(
        side_effect=lambda room_ids: {
            room_id: RoomCleanSettings() for room_id in room_ids
        }
    )

    def set_active_room_ids(room_ids: list[int] | None) -> None:
        coordinator.active_room_ids = room_ids

    coordinator.set_active_room_ids.side_effect = set_active_room_ids

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}

    # Stub StateVacuumEntity attributes
    vac.last_seen_segments = None
    vac.async_create_segments_issue = MagicMock()
    vac.async_write_ha_state = MagicMock()

    return vac


def _active_clean_state() -> NarwalState:
    """Return a state whose working_status still looks like active cleaning."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING_ALT
    state.last_active_working_status_time = time.monotonic()
    return state


class TestVacuumActivity:
    """Tests for mapping Narwal task context to HA vacuum activity."""

    def test_charging_to_resume_reports_cleaning_activity(self) -> None:
        state = _active_clean_state()
        state.battery_level = 25
        state.battery_level_increasing = True
        state.last_battery_change_time = time.monotonic()
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"
        state.task_progress_percent = 32
        vac = _make_vacuum(state=state)

        assert state.is_charging_to_resume
        assert vac.activity == VacuumActivity.CLEANING
        assert vac.extra_state_attributes["task_status"] == "charging_to_resume"
        assert (
            vac.extra_state_attributes["status_summary"]
            == "Cleaning - charging to resume - Kitchen - 32%"
        )
        assert vac.extra_state_attributes["docked"] is True

    def test_paused_returning_task_reports_paused_activity(self) -> None:
        state = _active_clean_state()
        state.is_paused = True
        state.is_returning_to_dock = True
        state.dock_sub_state = 2
        vac = _make_vacuum(state=state)

        assert state.is_returning
        assert vac.activity == VacuumActivity.PAUSED
        assert vac.extra_state_attributes["task_status"] == "paused"

    def test_remapping_reports_remapping_task_status(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.REMAPPING
        vac = _make_vacuum(state=state)

        assert vac.extra_state_attributes["task_status"] == "remapping"

    def test_parsed_fault_reports_error_activity_and_task_status(self) -> None:
        state = _active_clean_state()
        state.has_error = True
        state.error_codes = [2105]
        vac = _make_vacuum(state=state)

        assert vac.activity == VacuumActivity.ERROR
        assert vac.extra_state_attributes["task_status"] == "error"

    def test_dock_drying_during_open_task_reports_docked_activity(self) -> None:
        state = _active_clean_state()
        state.dry_mop_remaining_time = 12_503
        state.mop_drying_elapsed = 5_497
        state.mop_drying_target = 18_000
        state.dock_field11 = 3
        state.dock_field47 = 1
        vac = _make_vacuum(state=state)

        assert state.is_cleaning
        assert state.is_station_active
        assert vac.activity == VacuumActivity.DOCKED
        assert vac.extra_state_attributes["task_status"] == "station_active"
        assert vac.extra_state_attributes["docked"] is True

    def test_dock_washing_reports_docked_activity(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 3}, "11": 3, "47": 1})
        vac = _make_vacuum(state=state)

        assert vac.activity == VacuumActivity.DOCKED
        assert vac.extra_state_attributes["task_status"] == "station_active"
        assert vac.extra_state_attributes["docked"] is True

    def test_app_started_dock_bag_drying_keeps_details_on_dock_switches(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.update_from_working_status({"12": 9_000, "13": 18_000, "19": {}})
        vac = _make_vacuum(state=state)

        attrs = vac.extra_state_attributes

        assert vac.activity == VacuumActivity.DOCKED
        assert attrs["task_status"] == "station_active"
        assert "station_task" not in attrs
        assert "station_tasks" not in attrs
        assert "drying_time_left" not in attrs
        assert "dock_progress" not in attrs
        assert "dock_progress_display" not in attrs
        assert "dock_timer_fields" not in attrs

    def test_stale_clean_details_hidden_after_clean_ends(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.task_progress_percent = 72
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"
        state.cleaning_area = 12.5
        state.cleaning_time = 900
        state.station_activity = 4
        vac = _make_vacuum(state=state)

        attrs = vac.extra_state_attributes

        assert attrs["task_status"] == "station_active"
        assert "progress" not in attrs
        assert "current_room_id" not in attrs
        assert "current_room" not in attrs
        assert "active_room_ids" not in attrs
        assert "cleaning_area" not in attrs
        assert "cleaning_time" not in attrs

    def test_stale_dock_timer_does_not_hide_active_clean_controls(self) -> None:
        state = _active_clean_state()
        state.dry_mop_remaining_time = 600
        state.mop_drying_elapsed = 120
        state.mop_drying_target = 720
        state.dock_field11 = 1
        state.dock_field47 = 2
        vac = _make_vacuum(state=state)

        assert not state.is_station_active
        assert vac.activity == VacuumActivity.CLEANING
        assert vac.extra_state_attributes["task_status"] == "cleaning"
        assert vac.extra_state_attributes["status_summary"] == "Cleaning"

    def test_stale_dock_status_with_off_dock_signal_is_not_docked(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 1, "47": 2})
        vac = _make_vacuum(state=state)

        assert not state.is_docked
        assert vac.activity == VacuumActivity.IDLE
        assert vac.extra_state_attributes["task_status"] == "unknown"

    def test_native_plan_off_dock_stale_dock_status_reports_cleaning(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 1, "47": 2})
        state.native_plan_trajectory = [(1.0, 1.0), (2.0, 2.0)]
        state.native_plan_trajectory_updated = time.monotonic()
        vac = _make_vacuum(state=state)

        assert state.is_cleaning
        assert vac.activity == VacuumActivity.CLEANING
        assert vac.extra_state_attributes["task_status"] == "cleaning"

    def test_paused_native_plan_off_dock_reports_paused(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "2": 1}, "11": 1, "47": 2})
        state.native_plan_trajectory = [(1.0, 1.0), (2.0, 2.0)]
        state.native_plan_trajectory_updated = time.monotonic()
        vac = _make_vacuum(state=state)

        attrs = vac.extra_state_attributes

        assert state.is_paused
        assert state.has_paused_clean_task_context
        assert vac.activity == VacuumActivity.PAUSED
        assert attrs["task_status"] == "paused"
        assert attrs["busy"]
        assert not attrs["setup_available"]

    def test_active_clean_details_remain_visible(self) -> None:
        state = _active_clean_state()
        state.task_progress_percent = 72
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"
        state.task_remaining_time = 300
        state.cleaning_area = 12.5
        state.cleaning_time = 900
        vac = _make_vacuum(state=state)

        attrs = vac.extra_state_attributes

        assert attrs["progress"] == 72
        assert attrs["remaining_time"] == 300
        assert attrs["current_room_id"] == 4
        assert attrs["current_room"] == "Kitchen"
        assert attrs["status_summary"] == "Cleaning - Kitchen - 72%"
        assert attrs["active_room_ids"] == [4]
        assert attrs["cleaning_area"] == 12.5
        assert attrs["cleaning_time"] == 900


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
        state = NarwalState()
        state.map_data = MapData(rooms=[RoomInfo(room_id=11), RoomInfo(room_id=9)])
        state.working_status = WorkingStatus.DOCKED
        vac = _make_vacuum(state=state)
        settings = vac.coordinator.clean_settings
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
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
            route=settings.route,
            room_settings={
                11: RoomCleanSettings(),
                9: RoomCleanSettings(),
            },
        )
        assert vac.coordinator.active_room_ids == [11, 9]

    async def test_rejected_start_rooms_raises_and_preserves_active_rooms(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=MagicMock(result_code=3, success=False)
        )

        with pytest.raises(Exception, match="room clean failed"):
            await vac.async_clean_segments(["11"])

        assert vac.coordinator.active_room_ids is None

    async def test_empty_segment_ids_raise_without_starting_saved_plan(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()
        vac.coordinator.client.start = AsyncMock()

        with pytest.raises(Exception, match="must not be empty"):
            await vac.async_clean_segments([])

        vac.coordinator.client.start_rooms.assert_not_awaited()
        vac.coordinator.client.start.assert_not_called()

    async def test_segment_clean_revalidates_after_map_refresh(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        refreshed = _active_clean_state()
        vac.coordinator.client.state = refreshed

        with pytest.raises(Exception, match="room clean cannot be started"):
            await vac.async_clean_segments(["11"])

        vac.coordinator.client.start_rooms.assert_not_awaited()


class TestVacuumReturnToBase:
    async def test_preserves_active_room_plan_while_returning(self) -> None:
        """Return-to-base starts a return phase; the coordinator clears rooms later."""
        state = _active_clean_state()
        state.is_returning_to_dock = True
        state.dock_sub_state = 2
        vac = _make_vacuum(state=state)
        vac.coordinator.active_room_ids = [11, 9]
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.wake = AsyncMock()
        vac.coordinator.client.return_to_base = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )

        await vac.async_return_to_base()

        assert vac.coordinator.active_room_ids == [11, 9]
        vac.coordinator.set_active_room_ids.assert_not_called()
        vac.async_write_ha_state.assert_called_once()

    async def test_non_numeric_segment_id_raises(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(Exception, match="numeric"):
            await vac.async_clean_segments(["kitchen"])

        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_unknown_segment_id_raises(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(rooms=[RoomInfo(room_id=11)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(Exception, match="Unknown Narwal room ID"):
            await vac.async_clean_segments(["99"])

        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_rejected_return_to_base_raises(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.return_to_base = AsyncMock(
            return_value=MagicMock(result_code=3, success=False)
        )

        with pytest.raises(Exception, match="return to dock failed"):
            await vac.async_return_to_base()

        vac.async_write_ha_state.assert_not_called()


class TestVacuumControls:
    async def test_locate_unavailable_when_dock_task_active(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.dry_mop_remaining_time = 600
        state.dock_field11 = 3
        state.dock_field47 = 1
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.locate = AsyncMock()

        with pytest.raises(Exception, match="locate cannot be used"):
            await vac.async_locate()

        vac.coordinator.client.locate.assert_not_awaited()

    async def test_rejected_stop_raises(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock(
            return_value=MagicMock(result_code=3, success=False)
        )

        with pytest.raises(Exception, match="stop failed"):
            await vac.async_stop()

        assert vac.coordinator.active_room_ids is None

    async def test_stop_available_during_station_phase_of_active_clean(self) -> None:
        state = _active_clean_state()
        state.dry_mop_remaining_time = 600
        state.dock_field11 = 3
        state.dock_field47 = 1
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
        )

        await vac.async_stop()

        vac.coordinator.client.stop.assert_awaited_once()
        vac.coordinator.set_active_room_ids.assert_called_once_with(None)

    async def test_stop_revalidates_after_wake_refresh(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        vac.coordinator.client.stop = AsyncMock()

        refreshed = NarwalState()
        refreshed.working_status = WorkingStatus.DOCKED
        vac.coordinator.client.state = refreshed

        with pytest.raises(Exception, match="cannot be stopped"):
            await vac.async_stop()

        vac.coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
        vac.coordinator.client.stop.assert_not_awaited()

    async def test_pause_revalidates_after_wake_refresh(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        vac.coordinator.client.pause = AsyncMock()

        refreshed = NarwalState()
        refreshed.working_status = WorkingStatus.DOCKED
        vac.coordinator.client.state = refreshed

        with pytest.raises(Exception, match="cannot be paused"):
            await vac.async_pause()

        vac.coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
        vac.coordinator.client.pause.assert_not_awaited()

    async def test_return_to_base_revalidates_after_wake_refresh(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        vac.coordinator.client.return_to_base = AsyncMock()

        refreshed = NarwalState()
        refreshed.working_status = WorkingStatus.DOCKED
        vac.coordinator.client.state = refreshed

        with pytest.raises(Exception, match="cannot return"):
            await vac.async_return_to_base()

        vac.coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
        vac.coordinator.client.return_to_base.assert_not_awaited()

    async def test_locate_revalidates_after_wake_refresh(self) -> None:
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        vac.coordinator.client.locate = AsyncMock()

        refreshed = NarwalState()
        refreshed.working_status = WorkingStatus.DOCKED
        refreshed.dry_mop_remaining_time = 600
        refreshed.dock_field11 = 3
        refreshed.dock_field47 = 1
        vac.coordinator.client.state = refreshed

        with pytest.raises(Exception, match="locate cannot be used"):
            await vac.async_locate()

        vac.coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
        vac.coordinator.client.locate.assert_not_awaited()

    async def test_live_fan_change_applies_while_paused(self) -> None:
        state = _active_clean_state()
        state.is_paused = True
        vac = _make_vacuum(state=state)
        vac.coordinator.client.set_fan_speed = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
        )

        await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_awaited_once_with(FanLevel.STRONG)
        assert vac.coordinator.clean_settings.fan == FanLevel.STRONG

    async def test_fan_change_rejected_when_entity_unavailable(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.last_update_success = False
        vac.coordinator.client.set_fan_speed = AsyncMock()

        with pytest.raises(Exception, match="fan speed cannot be changed"):
            await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_not_awaited()

    async def test_fan_change_rejected_in_mop_only_mode(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.clean_settings.work_mode = WorkMode.MOP
        vac.coordinator.client.set_fan_speed = AsyncMock()

        with pytest.raises(Exception, match="mop-only mode"):
            await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_not_awaited()

    async def test_rejected_live_fan_change_does_not_update_settings(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.set_fan_speed = AsyncMock(
            return_value=MagicMock(result_code=2, success=False)
        )
        vac.coordinator.clean_settings.fan = FanLevel.NORMAL

        with pytest.raises(Exception, match="set fan speed failed"):
            await vac.async_set_fan_speed("Strong")

        assert vac.coordinator.clean_settings.fan == FanLevel.NORMAL

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

    def test_repeated_same_change_only_reports_once(self) -> None:
        """Does not spam repairs/logs for an unchanged segment mismatch."""
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
        vac._check_segment_changes()

        vac.async_create_segments_issue.assert_called_once()

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

    async def test_enumerates_all_rooms(self) -> None:
        """Whole-house start passes every room id to clean/start_clean, skipping plan/start."""
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(map_id=2, rooms=[
            RoomInfo(room_id=1), RoomInfo(room_id=2), RoomInfo(room_id=0),  # 0 filtered
        ])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )
        vac.coordinator.client.start = AsyncMock()

        await vac.async_start()

        vac.coordinator.client.start_rooms.assert_awaited_once()
        assert vac.coordinator.client.start_rooms.await_args.args[0] == [1, 2]
        vac.coordinator.client.start.assert_not_called()
        assert vac.coordinator.active_room_ids == [1, 2]

    async def test_whole_house_start_passes_room_profiles(self) -> None:
        """Whole-house start uses room-specific profiles for each room."""
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=1), RoomInfo(room_id=2)],
        )
        profile = RoomCleanSettings(fan=FanLevel.STRONG)
        vac = _make_vacuum(state=state)
        vac.coordinator.room_clean_settings_for_rooms = MagicMock(
            return_value={1: profile, 2: RoomCleanSettings()}
        )
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )

        await vac.async_start()

        kwargs = vac.coordinator.client.start_rooms.await_args.kwargs
        assert kwargs["room_settings"] == {1: profile, 2: RoomCleanSettings()}

    async def test_falls_back_to_saved_plan_without_map(self) -> None:
        """With no map rooms available, falls back to the saved-plan start()."""
        state = NarwalState()  # no map_data
        state.working_status = WorkingStatus.DOCKED
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.get_map = AsyncMock()  # does not populate map_data
        vac.coordinator.client.start = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        await vac.async_start()

        vac.coordinator.client.start.assert_awaited_once()
        vac.coordinator.client.start_rooms.assert_not_called()

    async def test_whole_house_start_revalidates_after_map_prefetch(self) -> None:
        """A stale idle state must not start a clean after map prefetch refreshes it."""
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True

        refreshed = _active_clean_state()

        async def refresh_map() -> None:
            vac.coordinator.client.state = refreshed

        vac.coordinator.client.get_map = AsyncMock(side_effect=refresh_map)
        vac.coordinator.client.start = AsyncMock()
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(Exception, match="clean cannot be started"):
            await vac.async_start()

        vac.coordinator.client.start.assert_not_called()
        vac.coordinator.client.start_rooms.assert_not_called()
