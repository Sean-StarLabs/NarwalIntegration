"""Tests for vacuum entity Segment API (room-specific cleaning).

Tests async_get_segments, async_clean_segments, and _check_segment_changes
on the NarwalVacuum entity using HA stubs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.coordinator import (  # noqa: E402
    CleanSettings,
    can_prepare_clean_start,
)
from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402
from narwal_client import RoomCleanSettings  # noqa: E402
from narwal_client.const import (  # noqa: E402
    CommandResult,
    FanLevel,
    WorkingStatus,
    WorkMode,
)
from narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    CommandResponse,
    MapData,
    NarwalState,
    RoomInfo,
)

Segment = sys.modules["homeassistant.components.vacuum"].Segment
VacuumActivity = sys.modules["homeassistant.components.vacuum"].VacuumActivity
VacuumEntityFeature = sys.modules["homeassistant.components.vacuum"].VacuumEntityFeature


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
    coordinator.active_clean_work_mode = None
    coordinator.has_selected_clean_rooms.return_value = False
    coordinator.active_clean_setting.return_value = None
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
    coordinator.async_prepare_clean_start = AsyncMock(return_value=True)
    coordinator.async_refresh_action_status = AsyncMock(return_value=True)
    coordinator.async_clear_map_display_cache = AsyncMock()
    coordinator.dock_action_lock = asyncio.Lock()
    coordinator.room_clean_settings_for_rooms = MagicMock(
        side_effect=lambda room_ids: {
            room_id: RoomCleanSettings() for room_id in room_ids
        }
    )
    coordinator.selected_clean_room_ids_for = MagicMock(
        side_effect=lambda room_ids: list(room_ids)
    )
    coordinator.compatible_room_clean_work_mode = MagicMock(
        side_effect=lambda room_settings: next(
            iter({settings.work_mode for settings in room_settings.values()})
        )
    )
    coordinator.record_accepted_clean_start = MagicMock(
        side_effect=lambda room_settings: setattr(
            coordinator,
            "active_clean_work_mode",
            next(iter({settings.work_mode for settings in room_settings.values()})),
        )
    )
    coordinator.clean_setting_applicability_mode = MagicMock(
        side_effect=lambda live=False: (
            coordinator.active_clean_work_mode
            if live and coordinator.active_clean_work_mode is not None
            else coordinator.clean_settings.work_mode
        )
    )

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}

    # Stub StateVacuumEntity attributes
    vac.last_seen_segments = None
    vac._last_reported_segment_signature = None
    vac.async_create_segments_issue = MagicMock()
    vac.async_write_ha_state = MagicMock()

    return vac


def _active_clean_state() -> NarwalState:
    """Return a state whose working_status still looks like active cleaning."""
    state = NarwalState()
    state.working_status = WorkingStatus.CLEANING_ALT
    return state


def _docked_state() -> NarwalState:
    """Return a state whose reported status is idle on the dock."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    return state


class TestVacuumActivity:
    """Tests for mapping Narwal task context to HA vacuum activity."""

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
        assert attrs["status_summary"] == "Kitchen - 72%"
        assert "charging_to_resume" not in attrs
        assert "progress_display" not in attrs
        assert "remaining_time" not in attrs
        assert "current_room_id" not in attrs
        assert attrs["current_room"] == "Kitchen"
        assert "active_room_ids" not in attrs
        assert "cleaning_area" not in attrs
        assert "cleaning_time" not in attrs

    def test_stale_clean_details_hidden_after_clean_ends(self) -> None:
        state = NarwalState(working_status=WorkingStatus.CHARGED)
        state.task_progress_percent = 72
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"
        state.cleaning_area = 12.5
        state.cleaning_time = 900
        vac = _make_vacuum(state=state)

        attrs = vac.extra_state_attributes

        assert attrs["task_status"] == "docked"
        assert "progress" not in attrs
        assert "remaining_time" not in attrs
        assert "current_room_id" not in attrs
        assert "current_room" not in attrs
        assert "active_room_ids" not in attrs
        assert "cleaning_area" not in attrs
        assert "cleaning_time" not in attrs

    def test_unknown_off_dock_status_reports_idle_without_task_fields(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 0}, "11": 1, "47": 2})
        vac = _make_vacuum(state=state)

        assert state.working_status == WorkingStatus.UNKNOWN
        assert not state.is_cleaning
        assert vac.activity == VacuumActivity.IDLE
        assert vac.extra_state_attributes["task_status"] == "unknown"

    def test_assumed_clean_reports_active_during_start_handoff(self) -> None:
        """An accepted start remains visible until native task telemetry arrives."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 0}, "11": 1, "47": 2})
        state.assume_robot_clean()
        vac = _make_vacuum(state=state)

        features = vac.supported_features
        attrs = vac.extra_state_attributes

        assert vac.activity == VacuumActivity.CLEANING
        assert attrs["task_status"] == "cleaning"
        assert attrs["status_summary"] == "Cleaning"
        assert "progress" not in attrs
        assert "current_room" not in attrs
        assert features & VacuumEntityFeature.STOP

    def test_station_activity_alone_does_not_report_robot_docked(self) -> None:
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.dock_presence = 2
        state.dock_field11 = 1
        state.dock_field47 = 2
        state.station_activity = 4
        vac = _make_vacuum(state=state)

        assert not state.is_docked
        assert state.is_station_active
        assert vac.activity == VacuumActivity.IDLE
        assert vac.extra_state_attributes["task_status"] == "station_active"


class TestVacuumSupportedFeatures:
    """Tests for dynamically exposed native HA vacuum features."""

    def test_idle_docked_exposes_start_features(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}, "11": 2})
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert features & VacuumEntityFeature.STATE
        assert features & VacuumEntityFeature.START
        assert features & VacuumEntityFeature.FAN_SPEED
        assert features & VacuumEntityFeature.CLEAN_AREA
        assert not features & VacuumEntityFeature.STOP
        assert not features & VacuumEntityFeature.PAUSE

    def test_docked_v2_with_off_dock_fields_exposes_return_home_not_start(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 2}, "11": 1, "47": 2})
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert vac.activity == VacuumActivity.IDLE
        assert features & VacuumEntityFeature.RETURN_HOME
        assert not features & VacuumEntityFeature.START
        assert not features & VacuumEntityFeature.CLEAN_AREA

    def test_active_clean_exposes_active_native_features(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert features & VacuumEntityFeature.STATE
        assert features & VacuumEntityFeature.PAUSE
        assert features & VacuumEntityFeature.STOP
        assert features & VacuumEntityFeature.RETURN_HOME
        assert features & VacuumEntityFeature.FAN_SPEED
        assert not features & VacuumEntityFeature.START
        assert not features & VacuumEntityFeature.CLEAN_AREA

    def test_off_dock_dock_bag_drying_exposes_return_home(self) -> None:
        """Dock-bag drying can continue while an off-dock robot returns home."""
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert features & VacuumEntityFeature.RETURN_HOME
        assert not features & VacuumEntityFeature.STOP

    def test_active_clean_with_dock_bag_drying_keeps_robot_controls(self) -> None:
        """A compatible dock task must not hide controls for an active off-dock clean."""
        state = _active_clean_state()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert features & VacuumEntityFeature.PAUSE
        assert features & VacuumEntityFeature.STOP
        assert features & VacuumEntityFeature.RETURN_HOME
        assert features & VacuumEntityFeature.LOCATE
        assert features & VacuumEntityFeature.FAN_SPEED

    def test_off_dock_mop_drying_hides_return_home(self) -> None:
        """Mop drying still blocks return-home until hardware proves otherwise."""
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.dock_presence = 6
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert not features & VacuumEntityFeature.RETURN_HOME

    def test_stoppable_dock_task_exposes_start_features(self) -> None:
        """Start stays exposed when it can first clear a safe dock blocker."""
        state = _docked_state()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert can_prepare_clean_start(state)
        assert features & VacuumEntityFeature.START
        assert features & VacuumEntityFeature.CLEAN_AREA

    def test_mixed_selected_room_profiles_hide_start_feature(self) -> None:
        """Native START is hidden when current room profiles cannot dispatch."""
        state = _docked_state()
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=11), RoomInfo(room_id=12)],
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.room_clean_settings_for_rooms = MagicMock(
            return_value={
                11: RoomCleanSettings(work_mode=WorkMode.MOP),
                12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
            }
        )
        vac.coordinator.compatible_room_clean_work_mode = MagicMock(
            side_effect=ValueError("Mixed Narwal room clean modes are not supported")
        )

        features = vac.supported_features

        assert not features & VacuumEntityFeature.START
        assert features & VacuumEntityFeature.CLEAN_AREA

    def test_empty_cached_map_hides_start_feature(self) -> None:
        """Native START is hidden when the cached map has no cleanable rooms."""
        state = _docked_state()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=0)])
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert not features & VacuumEntityFeature.START
        assert features & VacuumEntityFeature.CLEAN_AREA

    def test_recent_clean_with_unstoppable_dock_task_hides_stop(self) -> None:
        """Retained clean metrics must not expose generic stop over dry dust-bin."""
        state = NarwalState(working_status=WorkingStatus.CHARGED)
        state.update_from_working_status({"3": 120})
        state.dock_presence = 1
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        vac = _make_vacuum(state=state)

        features = vac.supported_features

        assert not features & VacuumEntityFeature.STOP


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
            route=settings.route,
            room_settings={
                11: RoomCleanSettings(),
                9: RoomCleanSettings(),
            },
        )
        assert state.has_assumed_robot_clean
        vac.async_write_ha_state.assert_called_once()

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

    async def test_rejects_room_clean_when_start_planner_fails(self) -> None:
        """Room clean requests are blocked when the start planner fails closed."""
        state = _docked_state()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=11)])
        state.station_activity = 1
        vac = _make_vacuum(state=state)
        vac.coordinator.async_prepare_clean_start = AsyncMock(return_value=False)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError):
            await vac.async_clean_segments(["11"])

        vac.coordinator.client.start_rooms.assert_not_awaited()
        vac.coordinator.async_prepare_clean_start.assert_awaited_once()

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
        vac.coordinator.record_accepted_clean_start.assert_called_once()

    async def test_mixed_room_modes_raise_before_segment_start(self) -> None:
        """Unsupported mixed-mode room profiles fail before dispatch."""
        state = _docked_state()
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=11), RoomInfo(room_id=12)],
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        room_settings = {
            11: RoomCleanSettings(work_mode=WorkMode.MOP),
            12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
        }
        vac.coordinator.room_clean_settings_for_rooms = MagicMock(
            return_value=room_settings
        )
        vac.coordinator.compatible_room_clean_work_mode = MagicMock(
            side_effect=ValueError("Mixed Narwal room clean modes are not supported")
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError, match="Mixed Narwal room clean modes"):
            await vac.async_clean_segments(["11", "12"])

        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_mixed_segment_modes_fail_before_dock_prepare(self) -> None:
        """Invalid segment profiles are rejected before a dock task is stopped."""
        state = _docked_state()
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=11), RoomInfo(room_id=12)],
        )
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.room_clean_settings_for_rooms = MagicMock(
            return_value={
                11: RoomCleanSettings(work_mode=WorkMode.MOP),
                12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
            }
        )
        vac.coordinator.compatible_room_clean_work_mode = MagicMock(
            side_effect=ValueError("Mixed Narwal room clean modes are not supported")
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError, match="Mixed Narwal room clean modes"):
            await vac.async_clean_segments(["11", "12"])

        vac.coordinator.async_prepare_clean_start.assert_not_awaited()
        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_segment_clean_without_map_fails_before_dock_prepare(self) -> None:
        """Segment commands do not use stale HA segment cache as command input."""
        state = _docked_state()
        state.map_data = None
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = [Segment(id="11", name="Bathroom")]
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.get_map = AsyncMock()
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError, match="Narwal map is not available"):
            await vac.async_clean_segments(["11"])

        vac.coordinator.async_prepare_clean_start.assert_not_awaited()
        vac.coordinator.client.start_rooms.assert_not_awaited()

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
            return_value=CommandResponse(result_code=0)
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


class TestVacuumFanSpeed:
    def test_fan_speed_reports_active_room_profile(self) -> None:
        """The vacuum entity reports the dispatched room fan while cleaning."""
        vac = _make_vacuum(state=_active_clean_state())
        vac.coordinator.clean_settings.fan = FanLevel.NORMAL
        vac.coordinator.active_clean_setting.return_value = FanLevel.STRONG

        assert vac.fan_speed == "Strong"

    async def test_restore_none_fan_speed_as_ai_suction(self) -> None:
        """HA persists AI fan_speed as None; restore it as FanLevel.UNSPECIFIED."""
        vac = _make_vacuum(state=_docked_state())
        vac.coordinator.clean_settings.fan = FanLevel.NORMAL

        with patch.object(
            vac,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(attributes={"fan_speed": None})),
        ):
            await vac.async_added_to_hass()

        assert vac.coordinator.clean_settings.fan == FanLevel.UNSPECIFIED

    async def test_live_fan_change_applies_while_paused(self) -> None:
        state = _active_clean_state()
        state.is_paused = True
        vac = _make_vacuum(state=state)
        vac.coordinator.client.set_fan_speed = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_awaited_once_with(FanLevel.STRONG)
        assert vac.coordinator.clean_settings.fan == FanLevel.STRONG
        vac.coordinator.set_active_clean_setting.assert_called_once_with(
            "fan", FanLevel.STRONG
        )

    async def test_fan_change_stages_when_unavailable_idle(self) -> None:
        """Idle pending fan changes do not need a live coordinator connection."""
        state = _docked_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.last_update_success = False
        vac.coordinator.client.set_fan_speed = AsyncMock()

        await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_not_awaited()
        assert vac.coordinator.clean_settings.fan == FanLevel.STRONG

    async def test_fan_change_rejected_when_entity_unavailable(self) -> None:
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.last_update_success = False
        vac.coordinator.client.set_fan_speed = AsyncMock()

        with pytest.raises(Exception, match="fan speed cannot be changed"):
            await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_not_awaited()

    async def test_live_fan_change_uses_active_room_mode(self) -> None:
        """Live fan gating follows the accepted room mode, not pending global mode."""
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.clean_settings.work_mode = WorkMode.MOP
        vac.coordinator.active_clean_work_mode = WorkMode.VACUUM
        vac.coordinator.client.set_fan_speed = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_awaited_once_with(FanLevel.STRONG)
        assert vac.coordinator.clean_settings.fan == FanLevel.STRONG

    async def test_live_fan_change_rejects_active_mop_mode(self) -> None:
        """Pending vacuum mode must not expose fan control for an active mop-only task."""
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.clean_settings.work_mode = WorkMode.VACUUM
        vac.coordinator.active_clean_work_mode = WorkMode.MOP
        vac.coordinator.client.set_fan_speed = AsyncMock()

        with pytest.raises(Exception, match="fan speed cannot be changed|mop-only"):
            await vac.async_set_fan_speed("Strong")

        vac.coordinator.client.set_fan_speed.assert_not_awaited()

    async def test_unknown_fan_speed_raises(self) -> None:
        """Unsupported suction labels must not silently no-op."""
        state = NarwalState(working_status=WorkingStatus.CHARGED)
        vac = _make_vacuum(state=state)
        vac.coordinator.client.set_fan_speed = AsyncMock()

        with pytest.raises(Exception, match="Unsupported Narwal fan speed"):
            await vac.async_set_fan_speed("Turbo")

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
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
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

    def test_segment_change_reports_once_per_signature(self) -> None:
        """Repeated coordinator updates for one mismatch do not spam repairs/logs."""
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

    def test_segment_change_reports_again_after_matching_snapshot(self) -> None:
        """A resolved mismatch clears the debounce for future room changes."""
        rooms_old = [
            Segment(id="1", name="Kitchen"),
            Segment(id="2", name="Bathroom"),
        ]
        changed = [
            RoomInfo(room_id=1, name="Kitchen", room_sub_type=4, category=1),
            RoomInfo(room_id=3, name="Study", room_sub_type=5, category=1),
        ]
        matching = [
            RoomInfo(room_id=1, name="Kitchen", room_sub_type=4, category=1),
            RoomInfo(room_id=2, name="Bathroom", room_sub_type=6, category=1),
        ]
        state = NarwalState()
        state.map_data = MapData(rooms=changed)
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = rooms_old

        vac._check_segment_changes()
        state.map_data = MapData(rooms=matching)
        vac._check_segment_changes()
        state.map_data = MapData(rooms=changed)
        vac._check_segment_changes()

        assert vac.async_create_segments_issue.call_count == 2

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
    """async_start runs a room-selection aware clean via clean/start_clean."""

    async def test_enumerates_all_rooms(self, caplog) -> None:
        """With no selected rooms, start passes every room id to clean/start_clean."""
        state = _docked_state()
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
        vac.coordinator.selected_clean_room_ids_for.assert_called_once_with([1, 2])
        vac.coordinator.client.start.assert_not_called()
        assert "Start command was rejected" not in caplog.text
        assert state.has_assumed_robot_clean

    async def test_start_uses_selected_rooms(self) -> None:
        """Selected room switches narrow the native vacuum start command."""
        state = _docked_state()
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=1), RoomInfo(room_id=2)],
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.selected_clean_room_ids_for = MagicMock(return_value=[2])
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )

        await vac.async_start()

        vac.coordinator.selected_clean_room_ids_for.assert_called_once_with([1, 2])
        vac.coordinator.room_clean_settings_for_rooms.assert_called_once_with([2])
        vac.coordinator.client.start_rooms.assert_awaited_once()
        assert vac.coordinator.client.start_rooms.await_args.args[0] == [2]

    async def test_whole_house_start_passes_room_profiles(self) -> None:
        """Whole-house start uses room-specific profiles for each room."""
        state = _docked_state()
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
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        await vac.async_start()

        kwargs = vac.coordinator.client.start_rooms.await_args.kwargs
        assert kwargs["room_settings"] == {1: profile, 2: RoomCleanSettings()}

    async def test_start_refreshes_action_status_before_resume(self) -> None:
        """Resume uses the action refresh path before deciding the command."""
        state = NarwalState(working_status=WorkingStatus.CLEANING)
        state.is_paused = True
        state.task_progress_percent = 40
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.resume = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        await vac.async_start()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.resume.assert_awaited_once()
        vac.coordinator.client.start_rooms.assert_not_awaited()

    async def test_whole_house_without_map_raises(self) -> None:
        """With no map rooms available, no ambiguous start command is sent."""
        state = _docked_state()  # no map_data
        vac = _make_vacuum(state=state)
        vac.last_seen_segments = [Segment(id="1", name="Bedroom")]
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.get_map = AsyncMock()  # does not populate map_data
        vac.coordinator.client.start = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError, match="room map is not available"):
            await vac.async_start()

        vac.coordinator.client.start.assert_not_awaited()
        vac.coordinator.client.start_rooms.assert_not_called()
        vac.coordinator.async_prepare_clean_start.assert_not_awaited()

    async def test_rejected_whole_house_start_raises_service_error(self) -> None:
        """Rejected whole-house starts fail the HA service call."""
        state = _docked_state()
        state.map_data = MapData(map_id=2, rooms=[RoomInfo(room_id=1)])
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        )

        with pytest.raises(HomeAssistantError, match="Narwal start command failed"):
            await vac.async_start()

    async def test_mixed_whole_house_modes_fail_before_dock_prepare(self) -> None:
        """Invalid all-room profiles are rejected before a dock task is stopped."""
        state = _docked_state()
        state.map_data = MapData(
            map_id=2,
            rooms=[RoomInfo(room_id=11), RoomInfo(room_id=12)],
        )
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.room_clean_settings_for_rooms = MagicMock(
            return_value={
                11: RoomCleanSettings(work_mode=WorkMode.MOP),
                12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
            }
        )
        vac.coordinator.compatible_room_clean_work_mode = MagicMock(
            side_effect=ValueError("Mixed Narwal room clean modes are not supported")
        )
        vac.coordinator.client.start_rooms = AsyncMock()

        with pytest.raises(HomeAssistantError, match="Mixed Narwal room clean modes"):
            await vac.async_start()

        vac.coordinator.async_prepare_clean_start.assert_not_awaited()
        vac.coordinator.client.start_rooms.assert_not_awaited()


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

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_awaited_once_with()

    async def test_stop_routes_active_clean_with_dock_bag_to_robot_stop(self) -> None:
        """Drying the dock bag does not make robot STOP ambiguous while cleaning."""
        state = _active_clean_state()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        vac.coordinator.client.stop_dock_task = AsyncMock()

        await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_awaited_once()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()

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

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_stop_rejects_unmapped_dock_activity_during_clean_context(self) -> None:
        """Unknown dock activity must not fall through to generic robot force-end."""
        state = NarwalState(working_status=WorkingStatus.CLEANING)

        state.dock_activity = 99
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock()

        with pytest.raises(HomeAssistantError, match="cannot be stopped safely"):
            await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_stop_routes_dry_dust_task_through_dock_policy(self) -> None:
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_presence = 6
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_awaited_once_with()

    async def test_stop_rejects_recent_clean_with_unstoppable_dry_dust(self) -> None:
        """Recent clean metrics must not route dry dust-bin through generic stop."""
        state = NarwalState(working_status=WorkingStatus.CHARGED)
        state.update_from_working_status({"3": 120})
        state.dock_presence = 1
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock()

        with pytest.raises(HomeAssistantError, match="cannot be stopped safely"):
            await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_stop_rejects_unmapped_dock_only_task(self) -> None:
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_presence = 6
        state.dock_activity = 99
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock()
        vac.coordinator.client.stop_dock_task = AsyncMock()

        with pytest.raises(HomeAssistantError, match="cannot be stopped safely"):
            await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_not_awaited()
        vac.coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_robot_stop_refreshes_action_status_before_stop(self) -> None:
        """Robot stop revalidates active task status under the action lock."""
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.stop = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        await vac.async_stop()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.stop.assert_awaited_once()

    async def test_pause_refreshes_action_status_before_pause(self) -> None:
        """Pause revalidates active task status under the action lock."""
        state = _active_clean_state()
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.pause = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        await vac.async_pause()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.pause.assert_awaited_once()

    async def test_return_home_refreshes_action_status_before_return(self) -> None:
        """Return-to-base revalidates dock and robot status under the action lock."""
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        vac = _make_vacuum(state=state)
        vac.coordinator.client.robot_awake = True
        vac.coordinator.client.return_to_base = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        await vac.async_return_to_base()

        vac.coordinator.async_refresh_action_status.assert_awaited_once()
        vac.coordinator.client.return_to_base.assert_awaited_once()
