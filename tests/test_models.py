"""Tests for narwal_client.models — state data models."""

from __future__ import annotations

import logging
import struct
import zlib
from unittest.mock import patch

from narwal_client.const import (
    TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
    TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME,
    CommandResult,
    WorkingStatus,
)
from narwal_client.models import (
    CommandResponse,
    MapData,
    NarwalState,
    ObstacleInfo,
    RoomInfo,
    _parse_obstacles,
)


def _make_compressed_grid(width: int, height: int, fill_value: int = 0) -> bytes:
    """Create a zlib-compressed protobuf-packed map grid."""
    raw_varints = bytearray()
    for _ in range(width * height):
        value = fill_value
        while value > 0x7F:
            raw_varints.append((value & 0x7F) | 0x80)
            value >>= 7
        raw_varints.append(value & 0x7F)

    length_varint = bytearray()
    value = len(raw_varints)
    while value > 0x7F:
        length_varint.append((value & 0x7F) | 0x80)
        value >>= 7
    length_varint.append(value & 0x7F)
    return zlib.compress(bytes([0x0A]) + bytes(length_varint) + bytes(raw_varints))


class TestCommandResponse:
    """Tests for Narwal command responses."""

    def test_success_accepts_zero_and_success_code(self) -> None:
        """Narwal uses several success codes for accepted commands."""
        assert CommandResponse(result_code=0).success
        assert CommandResponse(result_code=CommandResult.SUCCESS).success
        assert CommandResponse(result_code=CommandResult.APPLIED).success
        assert not CommandResponse(result_code=CommandResult.CONFLICT).success


class TestNarwalState:
    """Tests for NarwalState data model."""

    def test_default_state(self) -> None:
        state = NarwalState()
        assert state.working_status == WorkingStatus.UNKNOWN
        assert state.battery_level == 0
        assert state.firmware_version == ""
        assert not state.is_cleaning
        assert not state.is_docked
        assert not state.is_returning
        assert state.dock_state_unknown

    def test_update_from_working_status(self) -> None:
        """working_status topic sets cleaning metrics and marks active cleaning."""
        state = NarwalState()
        # Field 2 = coveredArea (float32, m²); field 13 = totalDryStationBagTime, ignored.
        state.update_from_working_status(
            {"2": _float_to_uint32(12.5), "3": 120, "13": 18000}
        )
        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.working_status == WorkingStatus.CLEANING
        assert state.has_recent_active_working_status

    def test_working_status_station_timers_do_not_mark_cleaning(self) -> None:
        """Station-only working_status timers are not active clean telemetry."""
        state = NarwalState()
        state.update_from_working_status({"13": 18000})
        assert state.working_status == WorkingStatus.UNKNOWN
        assert not state.has_recent_active_working_status

    def test_working_status_metrics_do_not_override_remapping(self) -> None:
        """A stale working_status packet must not hide an explicit remap state."""
        state = NarwalState(working_status=WorkingStatus.REMAPPING)

        state.update_from_working_status(
            {"2": _float_to_uint32(12.5), "3": 120}
        )

        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.working_status == WorkingStatus.REMAPPING
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning
        assert not state.is_docked

    def test_remapping_clears_stale_dock_signals(self) -> None:
        """REMAPPING is off-dock context even though it is not cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.dock_sub_state = 4
        state.dock_activity = 4

        state.update_from_base_status({"3": {"1": 7}})

        assert state.working_status == WorkingStatus.REMAPPING
        assert not state.is_cleaning
        assert not state.is_docked
        assert state.dock_sub_state == 0
        assert state.dock_activity == 0
        assert state.dock_field11 == 1
        assert state.dock_field47 == 2

    def test_working_status_metrics_do_not_override_custom_cleaning(self) -> None:
        """Status 17 should not be collapsed back to generic cleaning."""
        state = NarwalState(working_status=WorkingStatus.CUSTOM_CLEANING)

        state.update_from_working_status({"2": _float_to_uint32(12.5), "3": 120})

        assert state.working_status == WorkingStatus.CUSTOM_CLEANING
        assert state.has_recent_active_working_status
        assert state.is_cleaning

    def test_working_status_metrics_do_not_override_task_completed(self) -> None:
        """Stale metrics must not hide the return-to-dock phase."""
        state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
        state.is_returning_to_dock = True
        state.dock_sub_state = 2

        state.update_from_working_status({"2": _float_to_uint32(12.5), "3": 120})

        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.working_status == WorkingStatus.TASK_COMPLETED
        assert state.is_returning
        assert not state.has_recent_active_working_status
        assert state.dock_sub_state == 2

    def test_working_status_decodes_progress_and_remaining_time(self) -> None:
        """working_status reports progress/remaining time without an aux query."""
        state = NarwalState()

        state.update_from_working_status(
            {
                "1": _float_to_uint32(0.64),
                "3": 120,
                "4": 600,
            }
        )

        assert state.task_progress_percent == 64
        assert state.cleaning_time == 120
        assert state.task_remaining_time == 600

    def test_working_status_accepts_integer_progress_and_zero_remaining_time(self) -> None:
        """Progress may arrive as an integer percent and zero remaining is valid."""
        state = NarwalState()

        state.update_from_working_status({"1": 47, "4": 0})

        assert state.task_progress_percent == 47
        assert state.task_remaining_time == 0

    def test_terminal_dock_status_ignores_stale_positive_task_metrics(self) -> None:
        """A stale metrics packet after docking must not resurrect cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}, "11": 3, "47": 1})

        state.update_from_working_status(
            {"1": _float_to_uint32(1.0), "2": _float_to_uint32(12.5), "3": 900, "4": 0}
        )

        assert state.task_progress_percent == 100
        assert state.task_remaining_time == 0
        assert state.working_status == WorkingStatus.DOCKED
        assert state.is_docked
        assert not state.is_cleaning
        assert not state.has_recent_active_working_status

    def test_task_completed_is_returning_until_docked(self) -> None:
        """TASK_COMPLETED is a return phase, not editable idle time."""
        state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)

        assert state.is_returning
        assert not state.is_docked
        assert not state.is_cleaning

        state.update_from_base_status({"3": {"1": 19, "10": 1}, "11": 3, "47": 1})

        assert state.is_docked
        assert not state.is_returning

    def test_task_completed_clears_stale_dock_activity_while_off_dock(self) -> None:
        """Return-phase packets must not inherit stale station activity."""
        state = NarwalState()
        state.dock_activity = 4

        state.update_from_base_status({"3": {"1": 19, "10": 2}, "11": 1, "47": 2})

        assert state.working_status == WorkingStatus.TASK_COMPLETED
        assert state.is_returning
        assert not state.is_docked
        assert state.dock_activity == 0

    def test_clear_washing_task_clears_station_activity(self) -> None:
        """Stopping a station task clears non-washing station activities locally."""
        for station_activity in (1, 2, 3, 4):
            state = NarwalState()
            state.dock_activity = 3
            state.station_activity = station_activity

            state.clear_washing_task()

            assert state.dock_activity == 0
            assert state.station_activity == 0

    def test_zeroed_task_metrics_do_not_mark_cleaning(self) -> None:
        """A zeroed session counter is not evidence of an active clean.

        Field presence alone must not flip the entity to cleaning — a docked
        robot reporting timeConsuming=0 would otherwise be shown as running.
        """
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert state.is_docked

        state.update_from_working_status({"2": _float_to_uint32(0.0), "3": 0})

        assert not state.has_recent_active_working_status
        assert not state.is_cleaning
        assert state.is_docked
        assert state.working_status == WorkingStatus.DOCKED

    def test_non_cleaning_base_status_clears_stale_task_details(self) -> None:
        """Progress/current-room fields from the prior clean should not leak after dock."""
        state = NarwalState()
        state.task_progress_percent = 72
        state.task_elapsed_time = 900
        state.task_remaining_time = 300
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"

        state.update_from_base_status({"3": {"1": 10, "10": 1}})

        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert state.task_remaining_time == 0
        assert state.current_room_id is None
        assert state.current_room_aux_name == ""

    def test_paused_overlay_preserves_task_details(self) -> None:
        """A paused task still needs progress and current-room details."""
        state = NarwalState()
        state.task_progress_percent = 72
        state.task_elapsed_time = 900
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"

        state.update_from_base_status({"3": {"1": 1, "2": 1}})

        assert state.is_paused
        assert state.has_paused_clean_task_context
        assert state.task_progress_percent == 72
        assert state.task_elapsed_time == 900
        assert state.current_room_id == 4
        assert state.current_room_aux_name == "Kitchen"

    def test_clean_progress_info_updates_task_details(self) -> None:
        """info/get_clean_progress_info exposes active task progress details."""
        state = NarwalState()

        state.update_from_aux_status(
            TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
            {"2": {"1": 47, "3": 600, "6": 4}},
        )

        assert state.task_progress_percent == 47
        assert state.task_elapsed_time == 600
        assert state.current_room_id == 4

    def test_clean_progress_info_accepts_normalized_float_progress(self) -> None:
        """Progress can arrive as a native 0..1 float from the decoder."""
        state = NarwalState()

        state.update_from_aux_status(
            TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
            {"2": {"1": 0.64}},
        )

        assert state.task_progress_percent == 64

    def test_clean_progress_info_accepts_float32_bit_pattern_progress(self) -> None:
        """Progress can arrive as a uint32 float bit pattern from the decoder."""
        state = NarwalState()

        state.update_from_aux_status(
            TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
            {"2": {"1": _float_to_uint32(0.75)}},
        )

        assert state.task_progress_percent == 75

    def test_working_status_clears_stale_dock_fields_with_explicit_off_dock_signal(self) -> None:
        """Fresh task metrics override stale status only when base_status says off dock."""
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.update_from_base_status({"3": {"1": 10}, "11": 1, "47": 2})

        state.update_from_working_status({"3": 120})

        assert state.is_cleaning
        assert not state.is_docked
        assert state.dock_sub_state == 0
        assert state.dock_activity == 0
        assert state.dock_field11 == 1
        assert state.dock_field47 == 2

    def test_explicit_off_dock_signal_overrides_terminal_dock_status(self) -> None:
        """Current off-dock fields are stronger than stale docked work status."""
        state = NarwalState()

        state.update_from_base_status({"3": {"1": 10}, "11": 1, "47": 2})

        assert state.working_status == WorkingStatus.DOCKED
        assert state.has_explicit_off_dock_signal
        assert not state.is_docked

    def test_terminal_dock_status_ignores_stale_off_dock_fields(self) -> None:
        """A fresh terminal dock state should not inherit prior cleaning fields."""
        state = NarwalState()
        state.update_from_working_status({"3": 120})

        state.update_from_base_status({"3": {"1": 10}})

        assert state.working_status == WorkingStatus.DOCKED
        assert state.is_docked

    def test_update_from_base_status_cleaning(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(85.0)})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_cleaning
        assert state.battery_level == 85

    def test_unknown_off_dock_with_display_map_is_not_cleaning(self) -> None:
        """Display-map broadcasts render the map but do not prove active cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 0}, "11": 1, "47": 2})
        state.map_display_data = object()

        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_native_plan_with_explicit_off_dock_overrides_stale_dock_status(self) -> None:
        """Fresh native route plans prove activity when dock fields say off-dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 1, "47": 2})
        state.native_plan_trajectory = [(1.0, 1.0), (2.0, 2.0)]
        state.native_plan_trajectory_updated = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=110.0):
            assert state.has_recent_native_plan_activity
            assert state.has_explicit_off_dock_signal
            assert state.is_cleaning
            assert not state.is_docked

    def test_terminal_result_blocks_native_plan_cleaning_inference(self) -> None:
        """A stopped task must not be revived by retained route-plan frames."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status({"3": {"1": 14}, "11": 1, "47": 2, "15": 2})
        state.native_plan_trajectory = [(1.0, 1.0), (2.0, 2.0)]
        state.native_plan_trajectory_updated = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=110.0):
            assert state.has_terminal_task_result
            assert state.has_recent_native_plan_activity
            assert state.has_explicit_off_dock_signal
            assert not state.has_unfinished_charge_resume_context
            assert not state.is_cleaning

    def test_active_clean_with_low_battery_stays_cleaning_without_dock_evidence(self) -> None:
        """Low battery alone should not override active/off-dock cleaning state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(20.0)})

        assert state.has_off_dock_signal
        assert not state.is_charging_to_resume
        assert state.is_cleaning
        assert not state.is_docked

    def test_explicit_off_dock_low_battery_is_not_charging_to_resume(self) -> None:
        """Low battery alone is not enough when fresh fields place robot off-dock."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status(
            {"3": {"1": 4, "3": 2}, "2": _float_to_uint32(20.0), "11": 1, "47": 2}
        )

        assert state.has_explicit_off_dock_signal
        assert state.has_charge_resume_context
        assert not state.is_charging_to_resume
        assert state.is_cleaning

    def test_active_clean_with_rising_battery_and_dock_signal_is_charging_to_resume(
        self,
    ) -> None:
        """A rising battery plus dock telemetry marks a paused-to-recharge task."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(20.0)})
        state.update_from_base_status(
            {
                "3": {"1": 4},
                "2": _float_to_uint32(21.0),
                "11": 3,
                "47": 1,
            }
        )

        assert state.battery_recently_increasing
        assert state.has_dock_presence_signal
        assert state.is_charging_to_resume
        assert not state.is_docked

    def test_active_clean_on_low_battery_dock_is_charging_to_resume_after_restart(
        self,
    ) -> None:
        """Low-battery dock telemetry is enough when HA lost battery trend memory."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 4}, "2": _float_to_uint32(21.0), "11": 3, "47": 1}
        )

        assert not state.battery_recently_increasing
        assert state.has_dock_presence_signal
        assert state.is_charging_to_resume

    def test_charge_resume_context_survives_low_battery_station_overlay(self) -> None:
        """Dock/station overlays must not immediately erase an interrupted clean."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(25.0)})
        assert state.has_recent_active_task_context

        state.update_from_base_status(
            {"3": {"1": 14, "18": 2}, "2": _float_to_uint32(26.0), "11": 3, "47": 1}
        )

        assert state.working_status == WorkingStatus.CHARGED
        assert state.is_station_active
        assert state.has_recent_active_task_context
        assert state.has_charge_resume_context
        assert state.is_charging_to_resume

    def test_active_clean_with_station_task_can_be_charging_to_resume(self) -> None:
        """A station task can run while an interrupted clean charges to resume."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 4, "18": 2}, "2": _float_to_uint32(20.0), "11": 3, "47": 1}
        )
        state.update_from_base_status(
            {"3": {"1": 4, "18": 2}, "2": _float_to_uint32(21.0), "11": 3, "47": 1}
        )

        assert state.battery_recently_increasing
        assert state.is_station_active
        assert state.is_charging_to_resume

    def test_retained_clean_details_with_station_task_are_charging_to_resume(
        self,
    ) -> None:
        """Progress retained across a restart still identifies a suspended clean."""
        state = NarwalState()
        state.task_progress_percent = 5
        state.current_room_id = 3
        state.set_dock_drying_task("dry_dock_bag", 1_800, 18_000, ("12", "13"))
        state.update_from_base_status({"2": _float_to_uint32(28.0), "11": 3, "47": 1})

        assert state.is_station_active
        assert state.has_charge_resume_context
        assert state.is_charging_to_resume

    def test_dock_maintenance_is_not_charging_to_resume(self) -> None:
        """A dock-only maintenance phase stays station_active while charging."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 14, "18": 2}, "2": _float_to_uint32(20.0), "11": 3, "47": 1}
        )
        state.update_from_base_status(
            {"3": {"1": 14, "18": 2}, "2": _float_to_uint32(21.0), "11": 3, "47": 1}
        )

        assert state.battery_recently_increasing
        assert state.is_station_active
        assert not state.is_charging_to_resume

    def test_active_clean_with_falling_battery_is_not_charging_to_resume(self) -> None:
        """Ordinary active cleaning consumes battery and should stay as cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(85.0)})
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(84.0)})

        assert not state.battery_recently_increasing
        assert state.battery_recently_decreasing
        assert not state.is_charging_to_resume

    def test_low_battery_resumed_clean_is_not_charging_to_resume(self) -> None:
        """A resumed low-battery clean should not stay labelled as charging."""
        state = NarwalState()
        state.task_progress_percent = 47
        state.current_room_id = 4
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(13.0)})
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(12.0)})

        assert state.has_charge_resume_context
        assert state.battery_recently_decreasing
        assert not state.is_charging_to_resume

    def test_recent_native_plan_movement_beats_stale_station_overlay(self) -> None:
        """Fresh native planned-route movement wins over stale dock/station fields."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.station_activity = 2
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.last_native_plan_movement = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=110.0):
            assert state.has_dock_presence_signal
            assert state.has_recent_cleaning_trail_movement
            assert state.has_resumed_cleaning_motion
            assert state.is_cleaning
            assert not state.is_station_active
            assert not state.is_docked
            assert not state.is_charging_to_resume

    def test_recent_display_map_pose_movement_beats_stale_station_overlay(self) -> None:
        """Fresh robot pose movement wins even before the camera records a trail."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.station_activity = 2
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.last_map_robot_movement = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=110.0):
            assert state.has_dock_presence_signal
            assert state.has_recent_cleaning_trail_movement
            assert state.has_resumed_cleaning_motion
            assert state.is_cleaning
            assert not state.is_station_active
            assert not state.is_docked
            assert not state.is_charging_to_resume

    def test_confirmed_dock_after_map_movement_beats_resumed_motion(self) -> None:
        """A newer confirmed dock transition wins over prior map movement."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.last_map_robot_movement = 100.0
        state.last_confirmed_dock_time = 110.0

        with patch("narwal_client.models.time.monotonic", return_value=111.0):
            assert state.has_recent_cleaning_trail_movement
            assert not state.has_resumed_cleaning_motion
            assert state.is_docked
            assert state.is_charging_to_resume

    def test_native_trail_after_confirmed_dock_beats_stale_dock_state(self) -> None:
        """A native route recorded after dock telemetry proves resumed cleaning."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.last_confirmed_dock_time = 100.0
        state.last_native_plan_movement = 110.0

        with patch("narwal_client.models.time.monotonic", return_value=111.0):
            assert state.latest_cleaning_movement_time == 110.0
            assert state.has_resumed_cleaning_motion
            assert state.is_cleaning
            assert not state.is_docked
            assert not state.is_charging_to_resume

    def test_stale_native_trail_keeps_station_overlay(self) -> None:
        """Old route points should not hide a real dock-side phase."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.station_activity = 2
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.last_native_plan_movement = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=230.0):
            assert not state.has_recent_cleaning_trail_movement
            assert not state.has_resumed_cleaning_motion
            assert state.is_station_active
            assert state.is_charging_to_resume

    def test_display_trail_without_pose_or_plan_movement_keeps_station_overlay(self) -> None:
        """A visual display-map path alone should not prove off-dock movement."""
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.battery_level = 10
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.station_activity = 2
        state.dock_field11 = 3
        state.dock_field47 = 1
        state.cleaning_trail = [(float(index), 0.0) for index in range(30)]
        state.last_cleaning_trail_record = 110.0

        with patch("narwal_client.models.time.monotonic", return_value=111.0):
            assert not state.has_recent_cleaning_trail_movement
            assert not state.has_resumed_cleaning_motion
            assert state.is_station_active
            assert state.is_charging_to_resume

    def test_docked_charge_after_falling_battery_is_charging_to_resume(self) -> None:
        """A falling off-dock sample should not block a later confirmed dock charge."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status(
            {"3": {"1": 4, "3": 2}, "2": _float_to_uint32(31.0), "11": 1, "47": 2}
        )
        state.update_from_base_status(
            {"3": {"1": 14}, "2": _float_to_uint32(30.0), "11": 3, "47": 1}
        )

        assert state.battery_recently_decreasing
        assert not state.battery_recently_decreasing_without_dock_evidence
        assert state.has_dock_presence_signal
        assert state.is_charging_to_resume

    def test_completed_low_battery_dock_is_not_charging_to_resume(self) -> None:
        """Completed retained task details must not block new clean setup."""
        state = NarwalState()
        state.task_progress_percent = 100
        state.current_room_id = 4
        state.update_from_base_status(
            {"3": {"1": 14}, "2": _float_to_uint32(26.0), "11": 3, "47": 1}
        )

        assert not state.has_completed_task_context
        assert not state.has_charge_resume_context
        assert not state.has_unfinished_charge_resume_context
        assert not state.is_charging_to_resume
        assert state.is_docked

    def test_user_stopped_low_progress_dock_is_not_charging_to_resume(self) -> None:
        """A manual/forced stop is terminal even when progress is below 100%."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status(
            {
                "3": {"1": 14},
                "2": _float_to_uint32(30.0),
                "11": 3,
                "47": 1,
                "15": 2,
            }
        )

        assert state.has_terminal_task_result
        assert state.has_completed_task_context
        assert not state.has_unfinished_charge_resume_context
        assert not state.is_charging_to_resume
        assert state.is_docked

    def test_low_battery_force_end_can_charge_to_resume(self) -> None:
        """Low-battery force-end is the one terminal reason that can resume."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status(
            {
                "3": {"1": 14},
                "2": _float_to_uint32(30.0),
                "11": 3,
                "47": 1,
                "15": 4,
            }
        )

        assert not state.has_terminal_task_result
        assert state.has_unfinished_charge_resume_context
        assert state.is_charging_to_resume

    def test_new_active_clean_clears_previous_terminal_result(self) -> None:
        """A stale TaskResult must not poison a later clean's recharge phase."""
        state = NarwalState()
        state.task_progress_percent = 48
        state.current_room_id = 4
        state.update_from_base_status(
            {
                "3": {"1": 14},
                "2": _float_to_uint32(30.0),
                "11": 3,
                "47": 1,
                "15": 2,
            }
        )

        assert state.has_terminal_task_result
        assert state.has_completed_task_context

        state.task_progress_percent = 40
        state.current_room_id = 4
        state.update_from_base_status(
            {
                "3": {"1": 4},
                "2": _float_to_uint32(31.0),
                "11": 1,
                "47": 2,
                "15": 2,
            }
        )

        assert state.terminate_reason == 0
        assert not state.has_terminal_task_result
        assert state.has_unfinished_charge_resume_context
        assert state.is_cleaning

        state.update_from_base_status(
            {"3": {"1": 14}, "2": _float_to_uint32(30.0), "11": 3, "47": 1}
        )

        assert not state.has_terminal_task_result
        assert state.has_unfinished_charge_resume_context
        assert state.is_charging_to_resume

    def test_active_clean_with_high_rising_battery_is_not_charging_to_resume(self) -> None:
        """A high battery should not be treated as a mid-task recharge."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(80.0)})
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(81.0)})

        assert state.battery_recently_increasing
        assert not state.is_charging_to_resume

    def test_working_status_decodes_mop_drying_timer(self) -> None:
        """Flow 2 working-status fields 8/9 expose mop drying elapsed/target time."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.update_from_working_status({"8": 900, "9": 12600})

        assert state.mop_drying_elapsed == 900
        assert state.mop_drying_target == 12600
        assert state.dry_mop_remaining_time == 11700
        assert state.dock_drying_elapsed == 900
        assert state.dock_drying_target == 12600
        assert state.dock_drying_remaining_time == 11700
        assert state.active_dock_drying_task == "drying_mop"
        assert state.is_station_active

    def test_active_clean_clears_stale_mop_drying_timer(self) -> None:
        """Fresh off-dock clean metrics override a previously reported dock timer."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.update_from_working_status({"8": 900, "9": 12600})
        state.update_from_base_status({"3": {"1": 14}, "11": 1, "47": 2})

        state.update_from_working_status({"3": 120})

        assert state.is_cleaning
        assert not state.is_station_active
        assert not state.is_drying_mop
        assert state.dry_mop_remaining_time is None
        assert state.dock_drying_remaining_time is None

    def test_active_off_dock_status_ignores_stale_station_activity(self) -> None:
        """A stale station activity value must not hide an off-dock clean."""
        state = NarwalState()

        state.update_from_base_status(
            {"3": {"1": 4, "3": 2, "18": 1}, "11": 1, "47": 2}
        )

        assert state.is_cleaning
        assert state.station_activity == 0
        assert not state.is_station_active

    def test_working_status_station_timer_beats_stale_clean_elapsed(self) -> None:
        """Dock timer packets must not be reclassified as active robot cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 4}, "11": 3, "47": 1})

        state.update_from_working_status({"3": 600, "8": 900, "9": 12600})

        assert state.working_status == WorkingStatus.CHARGED
        assert state.is_station_active
        assert state.is_drying_mop
        assert state.active_dock_drying_task == "drying_mop"
        assert not state.is_cleaning
        assert state.dock_field11 == 3
        assert state.dock_field47 == 1

    def test_working_status_decodes_dock_bag_drying_timer(self) -> None:
        """Non-mop station timers expose a generic dock task without mop state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.update_from_working_status({"12": 9490, "13": 18000, "19": {}})

        assert state.mop_drying_elapsed == 0
        assert state.mop_drying_target == 0
        assert state.dry_mop_remaining_time is None
        assert state.dock_drying_elapsed == 9490
        assert state.dock_drying_target == 18000
        assert state.dock_drying_remaining_time == 8510
        assert state.dock_drying_progress_percent == 53
        assert state.dock_drying_timer_fields == ("12", "13")
        assert state.active_dock_drying_task == "dry_dock_bag"
        assert state.is_station_active

    def test_non_mop_timer_suppresses_coarse_mop_dry_activity(self) -> None:
        """Fresh dust drying timers beat stale dock_activity=4 mop fallback."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 4}, "11": 3, "47": 1})

        state.update_from_working_status({"10": 9_000, "11": 18_000, "19": {}})

        assert state.active_dock_drying_tasks == ("dry_dust_bin",)
        assert state.active_dock_drying_task == "dry_dust_bin"
        assert not state.is_drying_mop
        assert state.is_station_active

    def test_working_status_tracks_parallel_dock_timers(self) -> None:
        """Independent station timers should not collapse to a single task."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})

        state.update_from_working_status(
            {"8": 900, "9": 12600, "12": 9000, "13": 18000, "19": {}}
        )

        assert state.active_dock_drying_tasks == ("drying_mop", "dry_dock_bag")
        assert state.active_dock_drying_task == "drying_mop"
        assert state.dock_task_timer("drying_mop").progress_percent == 7
        assert state.dock_task_timer("dry_dock_bag").progress_percent == 50
        assert state.is_station_active

    def test_idle_base_status_clears_stale_dock_bag_timer(self) -> None:
        """An idle dock status clears old dock-bag timers after broadcasts stop."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
            state.update_from_working_status({"12": 10, "13": 18000, "19": {}})

        with patch("narwal_client.models.time.monotonic", return_value=150.0):
            state.update_from_base_status({"3": {"1": 1}, "11": 3, "47": 1})

        assert state.dock_drying_remaining_time is None
        assert state.active_dock_drying_task is None
        assert not state.is_station_active

    def test_idle_base_status_keeps_recent_dock_bag_timer(self) -> None:
        """Base status can be idle while a fresh station timer is still authoritative."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
            state.update_from_working_status({"12": 10, "13": 18000, "19": {}})

        with patch("narwal_client.models.time.monotonic", return_value=120.0):
            state.update_from_base_status({"3": {"1": 1}, "11": 3, "47": 1})

        assert state.active_dock_drying_task == "dry_dock_bag"
        assert state.is_station_active

    def test_assumed_dock_drying_task_is_bounded_and_clears_on_idle_status(self) -> None:
        """Polling-only command fallback should not become permanent state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})

        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.assume_dock_drying_task("dry_dock_bag")
            assert state.active_dock_drying_task == "dry_dock_bag"
            assert state.is_station_active

        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})

        assert state.active_dock_drying_task is None
        assert not state.is_station_active

    def test_assumed_dock_drying_task_expires(self) -> None:
        """A local command fallback is capped even without a later status."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})

        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.assume_dock_drying_task("dry_dock_bag")

        with patch("narwal_client.models.time.monotonic", return_value=100.0 + 6 * 60 * 60 + 1):
            assert state.active_dock_drying_task is None
            assert not state.is_station_active

    def test_base_status_dock_activity_3_is_washing_mop(self) -> None:
        """Live wash-and-dry capture reports mop washing as field 3.12 = 3."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 3}, "11": 3, "47": 1})

        assert state.is_docked
        assert state.is_station_active
        assert state.is_washing_mop

    def test_base_status_dock_activity_4_is_drying_mop(self) -> None:
        """Live wash-and-dry capture reports the timed drying phase as field 3.12 = 4."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 4}, "11": 3, "47": 1})

        assert state.is_docked
        assert state.is_station_active
        assert state.is_drying_mop

    def test_empty_dry_timer_suppresses_stale_dock_activity(self) -> None:
        """A dry-time query without a timer means dock_activity=4 is stale."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "12": 4}, "11": 3, "47": 1})

        state.update_from_aux_status(TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME, {})
        state.update_from_base_status({"3": {"1": 14, "12": 4}, "11": 3, "47": 1})

        assert state.is_docked
        assert not state.is_station_active
        assert not state.is_drying_mop
        assert state.dry_mop_remaining_time is None

        state.update_from_aux_status(TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME, {"2": 120})

        assert state.is_station_active
        assert state.is_drying_mop

    def test_empty_dry_timer_suppression_expires_by_age(self) -> None:
        """A later externally-started dry task should recover from stale suppression."""
        state = NarwalState(dock_activity=4)
        state.last_dry_mop_empty_time = 100.0

        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            assert not state.is_drying_mop
            assert not state.is_station_active

        with patch("narwal_client.models.time.monotonic", return_value=230.0):
            assert state.is_drying_mop
            assert state.is_station_active

    def test_empty_dry_timer_preserves_non_mop_station_activity(self) -> None:
        """A zero mop timer must not hide separate station drying/disinfection."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "18": 4}, "11": 3, "47": 1})

        state.update_from_aux_status(TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME, {})

        assert state.is_station_active
        assert not state.is_drying_mop
        assert state.station_activity == 4

    def test_dry_mop_timer_clears_stale_non_mop_timer_fields(self) -> None:
        """A dry-mop query must not inherit timer metadata from another dock task."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14}, "11": 3, "47": 1})
        state.update_from_working_status({"12": 9_000, "13": 18_000, "19": {}})

        state.update_from_aux_status(TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME, {"2": 600})

        assert state.active_dock_drying_task == "drying_mop"
        assert state.dock_drying_remaining_time == 600
        assert state.dock_drying_elapsed == 0
        assert state.dock_drying_target == 0
        assert state.dock_drying_timer_fields is None

    def test_working_status_ignores_non_numeric_station_timer_fields(self) -> None:
        """Field 12 can be a room-list shape in other payloads."""
        state = NarwalState()
        state.update_from_working_status({"12": {"1": 1}, "13": 18000})

        assert state.mop_drying_elapsed == 0
        assert state.mop_drying_target == 0
        assert state.dry_mop_remaining_time is None
        assert state.dock_drying_remaining_time is None

    def test_update_from_base_status_docked(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert state.working_status == WorkingStatus.DOCKED
        assert state.is_docked

    def test_update_from_base_status_charged(self) -> None:
        """Status 14 = fully charged on dock."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 14, "10": 1},
            "2": _float_to_uint32(100.0),
            "38": 100,
        })
        assert state.working_status == WorkingStatus.CHARGED
        assert state.is_docked
        assert state.battery_level == 100
        assert state.curing_agent_consumption_percent == 100

    def test_update_from_base_status_standby_on_dock(self) -> None:
        """STANDBY(1) with dock sub-state=1 means docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "10": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_off_dock_field11(self) -> None:
        """STANDBY(1) with field 11=1 means off dock (validated via dock_research)."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 1, "3": 2}, "11": 1, "47": 2,
            "2": _float_to_uint32(100.0),
        })
        assert state.working_status == WorkingStatus.STANDBY
        assert state.dock_field11 == 1
        assert state.dock_field47 == 2
        assert not state.is_docked

    def test_update_from_base_status_standby_on_dock_field11(self) -> None:
        """STANDBY(1) with field 11=2 means on dock (validated via dock_research).

        5 captures: field 11=2 in all 3 on-dock, field 11=1 in both off-dock.
        """
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 1, "3": 6}, "11": 2, "47": 3,
        })
        assert state.working_status == WorkingStatus.STANDBY
        assert state.dock_field11 == 2
        assert state.dock_field47 == 3
        assert state.is_docked

    def test_update_from_base_status_standby_on_dock_field47_only(self) -> None:
        """STANDBY(1) with field 47=3 means on dock (secondary signal)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "47": 3})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_no_signals(self) -> None:
        """STANDBY(1) with no dock signals at all — NOT docked (safe default)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert not state.is_docked

    def test_update_from_base_status_standby_dock_activity(self) -> None:
        """STANDBY(1) with dock_activity > 0 means docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "12": 2}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_paused(self) -> None:
        """Paused overlay: field 3 sub-field 2 = 1."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "2": 1}})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_paused
        assert not state.is_cleaning  # is_cleaning is False when paused

    # --- v01.07.23+ firmware tests ---

    def test_docked_v2_working_status(self) -> None:
        """DOCKED_V2(2) on v01.07.23+ firmware maps to docked."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2, "4": 1, "11": 3},  # new FW sub-fields
            "11": 3, "47": 1,
        })
        assert state.working_status == WorkingStatus.DOCKED_V2
        assert state.is_docked

    def test_cleaning_v2_working_status(self) -> None:
        """CLEANING_V2(3) on newer Flow 2 firmware maps to active cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 3}, "11": 1, "47": 2})
        assert state.working_status == WorkingStatus.CLEANING_V2
        assert state.is_cleaning
        assert not state.is_docked

    def test_custom_cleaning_working_status(self) -> None:
        """CUSTOM_CLEANING(17) maps to active cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 17}, "11": 1, "47": 2})
        assert state.working_status == WorkingStatus.CUSTOM_CLEANING
        assert state.is_cleaning
        assert not state.is_docked

    def test_new_fw_field3_unknown_subfields_logged(self) -> None:
        """New firmware sub-fields (4, 11) are parsed without error."""
        state = NarwalState()
        # Should not raise — unknown sub-fields logged at debug level
        state.update_from_base_status({"3": {"1": 2, "4": 99, "11": 3}})
        assert state.working_status == WorkingStatus.DOCKED_V2

    def test_new_fw_dock_field11_gte2(self) -> None:
        """v01.07.23 dock_field11=3 detected as docked via >= 2 check."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "11": 3})
        assert state.dock_field11 == 3
        assert state.is_docked

    def test_new_fw_dock_field47_eq1(self) -> None:
        """v01.07.23 dock_field47=1 detected as docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "47": 1})
        assert state.dock_field47 == 1
        assert state.is_docked

    def test_field3_as_list_parsed(self) -> None:
        """bbp can return field3 as a list — first element should be used."""
        state = NarwalState()
        state.update_from_base_status({"3": [{"1": 4, "2": 1}]})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_paused

    def test_field3_empty_list_no_crash(self) -> None:
        """Empty list for field3 should not crash."""
        state = NarwalState()
        state.update_from_base_status({"3": []})
        assert state.working_status == WorkingStatus.UNKNOWN  # unchanged default

    def test_field3_not_dict_no_crash(self) -> None:
        """Non-dict field3 (e.g., bytes from bbp) should not crash."""
        state = NarwalState()
        state.update_from_base_status({"3": b"\x08\x02"})
        assert state.working_status == WorkingStatus.UNKNOWN  # unchanged default

    def test_absent_paused_subfield_resets_to_false(self) -> None:
        """When field3.2 is absent (protobuf default=0), is_paused resets."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "2": 1}})
        assert state.is_paused
        # Next broadcast without "2" key → paused resets to False
        state.update_from_base_status({"3": {"1": 4}})
        assert not state.is_paused

    def test_unknown_working_status_value(self) -> None:
        """Unmapped working_status value falls back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 254}})
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_unknown_working_status_warns_once(self, caplog) -> None:
        """Repeated unknown values warn once, not once per broadcast (#46).

        The robot rebroadcasts status every ~1.5s; warning each time floods
        the log with thousands of identical lines.
        """
        from narwal_client import models as models_mod

        models_mod._WARNED_WORKING_STATUS.discard(255)
        state = NarwalState()
        with caplog.at_level(logging.WARNING, logger=models_mod.__name__):
            for _ in range(50):
                state.update_from_base_status({"3": {"1": 255}})

        warnings = [r for r in caplog.records if "Unknown working_status" in r.message]
        assert len(warnings) == 1
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_update_from_base_status(self) -> None:
        state = NarwalState()
        state.update_from_base_status({
            "2": _float_to_uint32(85.0),
            "38": 100,
            "36": 1757252225,
            "13": "d4bec8c82c484a3ba0428bb0dd4359e2",
        })
        assert state.battery_level == 85
        assert state.curing_agent_consumption_percent == 100
        assert state.station_bag_health_reset_time == 1757252225
        assert state.binded_uuid == "d4bec8c82c484a3ba0428bb0dd4359e2"

    def test_base_status_consumables_and_error(self) -> None:
        """Field 35 dust-bag health (float32 %), 41 detergent %, 1 errorCode presence."""
        state = NarwalState()
        state.update_from_base_status({
            "1": {},  # empty errorCode = no fault
            "35": _float_to_uint32(68.5),
            "41": 100,
        })
        assert round(state.dust_bag_health, 1) == 68.5
        assert state.detergent_remaining == 100
        assert state.has_error is False
        assert state.error_codes == []
        # A populated ErrorCode flips has_error on and exposes code/level/detail.
        state.update_from_base_status({"1": {"1": 2105, "2": 3, "3": b"wheel stuck"}})
        assert state.has_error is True
        assert state.error_codes == [2105]
        assert state.error_level == 3
        assert state.error_detail == "wheel stuck"
        # Clears when the next base_status reports an empty errorCode.
        state.update_from_base_status({"1": {}})
        assert state.has_error is False
        assert state.error_codes == []

    def test_multiple_error_codes(self) -> None:
        """Repeated ErrorCode (bbp list) collects all identityCodes, max level."""
        state = NarwalState()
        state.update_from_base_status({"1": [{"1": 10, "2": 1}, {"1": 20, "2": 4}]})
        assert state.error_codes == [10, 20]
        assert state.error_level == 4
        assert state.has_error is True

    def test_base_status_tank_states(self) -> None:
        """Tank/bag enum states parse into Optional ints; unreported stays None."""
        state = NarwalState()
        # Live healthy snapshot: clean-water/sewage ok (1), dust box ok (1),
        # station bag installed (1). No dust-bag field on this model.
        state.update_from_base_status({"23": 1, "24": 1, "20": 1, "39": 1})
        assert state.clean_water_tank_state == 1
        assert state.sewage_tank_state == 1
        assert state.dust_box_state == 1
        assert state.station_bag_state == 1
        assert state.dust_bag_state is None  # not reported by this model
        # Attention states.
        state.update_from_base_status({"23": 2, "39": 3})
        assert state.clean_water_tank_state == 2  # EMPTY
        assert state.station_bag_state == 3  # SUGGEST_REPLACE

    def test_terminate_reason(self) -> None:
        """Field 15 = terminateReason (TaskResult)."""
        state = NarwalState()
        state.update_from_base_status({"15": 4})
        assert state.terminate_reason == 4  # LOW_BATTERY_FORCE_END

    def test_consumable_info_parse(self) -> None:
        """consumable/get_consumable_info → maintain/replace alert lists; empty clears."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": [1, 9], "2": 8}})
        assert state.maintain_items == [1, 9]  # dust_box, water_tank_sponge
        assert state.replace_items == [8]  # dust_bag
        state.update_from_consumable_info({"1": {}})  # healthy
        assert state.maintain_items == []
        assert state.replace_items == []

    def test_consumable_info_packed_varints(self) -> None:
        """The wire shape: protobuf packs repeated scalars, bbp yields str (#79).

        Verbatim capture from a Flow (AX12, v01.08.03.07). The list-and-int shapes
        above are what a hand-written payload looks like, not what a robot sends —
        which is how this went unnoticed while every alert was dropped.
        """
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\x04\x06\x08\n", "2": "\x03\x14"}})
        # wash ribs, universal wheel, side distance sensor, anti-winding brush
        assert state.maintain_items == [4, 6, 8, 10]
        assert state.replace_items == [3, 20]  # side brush, station bag

    def test_consumable_info_single_packed_item(self) -> None:
        """A one-item packed list is where an off-by-one decoder still looks right."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\x02", "2": "\x08"}})
        assert state.maintain_items == [2]  # dust filter
        assert state.replace_items == [8]  # dust bag

    def test_consumable_info_accepts_bytes(self) -> None:
        """Same blob as bytes rather than str — decoder version shouldn't matter."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": b"\x04\x06", "2": b"\x14"}})
        assert state.maintain_items == [4, 6]
        assert state.replace_items == [20]

    def test_consumable_info_multibyte_varint(self) -> None:
        """Values above 127 span bytes; a byte-per-value shortcut would misread them."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\xac\x02", "2": "\x01"}})
        assert state.maintain_items == [300]
        assert state.replace_items == [1]

    def test_consumable_info_empty_blob_is_healthy(self) -> None:
        """An empty packed field still means nothing needs attention."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": [4], "2": [20]}})
        state.update_from_consumable_info({"1": {"1": "", "2": ""}})
        assert state.maintain_items == []
        assert state.replace_items == []

    def test_base_status_dock_light_mode(self) -> None:
        """Field 50 exposes the base station ambient light mode."""
        state = NarwalState()
        state.update_from_base_status({"50": 2})
        assert state.dock_light_mode == 2

    def test_base_status_missing_dock_light_means_off(self) -> None:
        """When the dock omits field 50, the ambient light is off."""
        state = NarwalState()
        state.update_from_base_status({"50": 2})
        state.update_from_base_status({"2": _float_to_uint32(100.0)})
        assert state.dock_light_mode == 0

    def test_update_from_upgrade_status(self) -> None:
        state = NarwalState()
        state.update_from_upgrade_status({
            "7": "v01.02.19.02",
            "8": "v01.02.19.02",
            "2": 3,
            "4": 10,
        })
        assert state.firmware_version == "v01.02.19.02"
        assert state.firmware_target == "v01.02.19.02"
        assert state.upgrade_status == 3
        assert state.upgrade_stage == 10

    def test_update_from_download_status(self) -> None:
        # Field 3 = state (field 1 is download type, ignored).
        state = NarwalState()
        state.update_from_download_status({"1": 5, "3": 2})
        assert state.download_status == 2

    def test_incremental_updates(self) -> None:
        """State should accumulate across multiple topic updates."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(95.0)})
        state.update_from_working_status({"3": 120, "2": _float_to_uint32(12.5)})
        state.update_from_upgrade_status({"7": "v01.02.19.02"})

        assert state.battery_level == 95
        assert state.is_cleaning
        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.firmware_version == "v01.02.19.02"

    def test_raw_data_preserved(self) -> None:
        state = NarwalState()
        raw = {"2": _float_to_uint32(100.0), "38": 100, "47": 2, "unknown_field": "value"}
        state.update_from_base_status(raw)
        assert state.raw_base_status == raw

    def test_battery_field2_float32_83(self) -> None:
        """Field 2 = 1118175232 → 83.0% battery (confirmed from monitor capture)."""
        state = NarwalState()
        state.update_from_base_status({"2": 1118175232})
        assert state.battery_level == 83

    def test_battery_field2_float32_85(self) -> None:
        """Field 2 = 1118437376 → 85.0% battery."""
        state = NarwalState()
        state.update_from_base_status({"2": 1118437376})
        assert state.battery_level == 85

    def test_battery_field2_as_python_float(self) -> None:
        """bbp may return field 2 as a Python float directly."""
        state = NarwalState()
        state.update_from_base_status({"2": 83.0})
        assert state.battery_level == 83

    def test_field38_curing_agent_not_battery(self) -> None:
        """Field 38 is curingAgentConsumptionPercent, not battery SOC/health."""
        state = NarwalState()
        state.update_from_base_status({"38": 100})
        assert state.curing_agent_consumption_percent == 100
        # battery_level unchanged (no field 2)
        assert state.battery_level == 0

    def test_battery_only_update_ignores_working_status(self) -> None:
        """update_battery_from_base_status updates battery but NOT working_status.

        When robot is in deep sleep, get_status() returns current battery
        but stale working_status. The battery-only method must not overwrite
        the last authoritative working_status.
        """
        state = NarwalState()
        # Simulate last authoritative state from a broadcast: DOCKED
        state.update_from_base_status({
            "3": {"1": 10, "10": 1},
            "2": _float_to_uint32(80.0),
        })
        assert state.working_status == WorkingStatus.DOCKED
        assert state.battery_level == 80

        # Now simulate a deep-sleep get_status() response with stale CLEANING
        # but fresh battery. Use battery-only update.
        stale_response = {
            "3": {"1": 4, "7": 1},  # stale CLEANING+returning
            "2": _float_to_uint32(85.0),
            "38": 100,
        }
        state.update_battery_from_base_status(stale_response)

        # Battery updated, working_status preserved from last authoritative source
        assert state.battery_level == 85
        assert state.curing_agent_consumption_percent == 100
        assert state.working_status == WorkingStatus.DOCKED  # NOT overwritten
        assert state.is_docked  # still correct

    def test_returning_to_dock_field7(self) -> None:
        """Field 3.7=1 indicates returning to dock (confirmed live)."""
        state = NarwalState()
        # Live data: {1=4, 7=1, 10=2} — CLEANING + returning + docking
        state.update_from_base_status({"3": {"1": 4, "7": 1, "10": 2}})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_returning_to_dock
        assert state.dock_sub_state == 2
        assert state.is_returning  # should be True via field 3.7
        assert not state.is_cleaning  # returning takes priority

    def test_returning_clears_when_docked(self) -> None:
        """Returning flag clears when robot docks."""
        state = NarwalState()
        # During return
        state.update_from_base_status({"3": {"1": 4, "7": 1, "10": 2}})
        assert state.is_returning
        # After docking: {1=14, 12=2}
        state.update_from_base_status({"3": {"1": 14, "12": 2}})
        assert not state.is_returning
        assert state.is_docked
        assert state.dock_activity == 2

    def test_returning_via_dock_sub_state_only(self) -> None:
        """dock_sub_state=2 alone is NOT enough — both field 3.7 AND 3.10 required."""
        state = NarwalState()
        # Only dock_sub_state=2 without field 3.7 — should NOT be returning
        # (single stale field causes false positives during normal cleaning)
        state.update_from_base_status({"3": {"1": 4, "10": 2}})
        assert not state.is_returning

    def test_not_returning_when_standby_with_dock_sub_state(self) -> None:
        """STANDBY with dock_sub_state=2 means docked, not returning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "10": 2}})
        assert not state.is_returning

    def test_not_returning_when_cleaning_without_field7(self) -> None:
        """Cleaning without field 3.7 is NOT returning (just cleaning)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}})
        assert state.is_cleaning
        assert not state.is_returning

    def test_second_unknown_working_status_value(self) -> None:
        """Unknown status values should fall back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 255}})
        assert state.working_status == WorkingStatus.UNKNOWN


def _float_to_uint32(f: float) -> int:
    """Encode a float as the uint32 bit pattern (for protobuf simulation)."""
    return struct.unpack("I", struct.pack("f", f))[0]


class TestMapData:
    """Tests for MapData.from_response()."""

    def test_basic_map_parsing(self) -> None:
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "12": [{"1": 3, "2": 0, "3": b"Kitchen"}],
            "17": b"\x78\x01" + b"\x00" * 20,
            "33": 944,
            "34": 1740000000,
        }}
        m = MapData.from_response(decoded)
        assert m.width == 341
        assert m.height == 494
        assert m.resolution == 60
        assert len(m.rooms) == 1
        assert m.rooms[0].name == "Kitchen"
        assert m.area == 944

    def test_dock_position_from_field8_uint32(self) -> None:
        """Dock parsed from field 8 (dm coords as uint32, same as display_map field 5)."""
        decoded = {
            "2": {
                "3": 60,
                "4": 341,
                "5": 494,
                "6": {"1": -341, "2": 152, "3": -280, "4": 60},
                "8": {
                    "1": {
                        "1": _float_to_uint32(-8.0188),
                        "2": _float_to_uint32(0.221),
                    },
                    "2": _float_to_uint32(0.036),
                },
                "17": b"",
            }
        }
        m = MapData.from_response(decoded)
        # factor 100 / 60: -8.0188 * 1.667 - (-280) = 266.64
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 266.6) < 1.0
        assert abs(m.dock_y - 341.4) < 1.0

    def test_dock_position_from_field8_float(self) -> None:
        """bbp may return fixed32 fields as Python floats directly."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "8": {"1": {"1": -8.0188, "2": 0.221}, "2": 0.036},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        # factor 100 / 60: -8.0188 * 1.667 - (-280) = 266.64
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 266.6) < 1.0
        assert abs(m.dock_y - 341.4) < 1.0

    def test_dock_position_missing_field8(self) -> None:
        """No dock position when field 8 is missing."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_dock_position_zero_resolution(self) -> None:
        """No dock position when resolution is zero."""
        decoded = {"2": {
            "3": 0,
            "4": 341,
            "5": 494,
            "8": {"1": {"1": -8.0, "2": 0.2}, "2": 0.0},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_consumable_info_success_marks_endpoint_available(self) -> None:
        state = NarwalState()

        state.update_from_consumable_info({"1": {"1": [4], "2": [20]}})

        assert state.consumable_info_available
        assert state.maintain_items == [4]
        assert state.replace_items == [20]

    def test_empty_response(self) -> None:
        m = MapData.from_response({})
        assert m.width == 0
        assert m.dock_x is None

    def test_obstacles_from_field32(self) -> None:
        """MapData.from_response includes obstacles parsed from field 32."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "17": b"",
            "32": {
                "1": [
                    {
                        "1": 1,
                        "2": 14,
                        "3": {"1": {"1": _float_to_uint32(-110.5), "2": _float_to_uint32(-129.5)}, "2": _float_to_uint32(11.0), "3": _float_to_uint32(41.0)},
                        "4": _float_to_uint32(180.0),
                    },
                ],
            },
        }}
        m = MapData.from_response(decoded)
        assert len(m.obstacles) == 1
        obs = m.obstacles[0]
        assert obs.id == 1
        assert obs.type_id == 14
        assert obs.display_name == "Sofa"
        assert abs(obs.center_x - (-110.5)) < 0.5
        assert abs(obs.center_y - (-129.5)) < 0.5
        assert abs(obs.width - 11.0) < 0.5
        assert abs(obs.height - 41.0) < 0.5

    def test_obstacles_empty_when_no_field32(self) -> None:
        """MapData.from_response returns empty obstacles when field 32 is missing."""
        decoded = {"2": {"3": 60, "4": 10, "5": 10, "17": b""}}
        m = MapData.from_response(decoded)
        assert m.obstacles == []

    def test_field26_room_boundaries_are_not_reported_as_carpets(self) -> None:
        """Map field 26 is room-boundary geometry, not rug/carpet metadata."""
        decoded = {
            "2": {
                "3": 60,
                "4": 10,
                "5": 10,
                "12": [{"1": 1, "2": 0, "3": b"Kitchen"}],
                "17": b"",
                "26": {
                    "1": 1,
                    "2": [
                        {"1": 0, "2": 0},
                        {"1": 0, "2": 9},
                        {"1": 9, "2": 9},
                        {"1": 9, "2": 0},
                    ],
                },
            }
        }

        m = MapData.from_response(decoded)

        assert m.carpets == []
        assert m.room_surfaces == {}

    def test_room_surfaces_unknown_without_confirmed_carpet_source(self) -> None:
        """Room pixels alone must not label every room as hard floor."""
        decoded = {
            "2": {
                "3": 60,
                "4": 4,
                "5": 4,
                "12": [{"1": 1, "2": 0, "3": b"Kitchen"}],
                "17": _make_compressed_grid(4, 4, fill_value=1 << 8),
            }
        }

        m = MapData.from_response(decoded)

        assert m.room_bounds == {1: (0, 0, 3, 3)}
        assert m.room_surfaces == {}


class TestObstacleInfo:
    """Tests for ObstacleInfo dataclass."""

    def test_display_name_known_type(self) -> None:
        """ObstacleInfo with type_id=14 has display_name 'Sofa'."""
        obs = ObstacleInfo(id=1, type_id=14)
        assert obs.display_name == "Sofa"

    def test_display_name_unknown_type(self) -> None:
        """ObstacleInfo with unknown type_id=99 has display_name 'Object 99'."""
        obs = ObstacleInfo(id=1, type_id=99)
        assert obs.display_name == "Object 99"

    def test_display_name_all_known_types(self) -> None:
        """All known type IDs have correct display names."""
        expected = {2: "Double Bed", 4: "Dining Table", 6: "Tea Table", 14: "Sofa", 28: "Toilet"}
        for type_id, name in expected.items():
            obs = ObstacleInfo(id=1, type_id=type_id)
            assert obs.display_name == name

    def test_to_grid_coords(self) -> None:
        """to_grid_coords subtracts origin correctly."""
        obs = ObstacleInfo(id=1, type_id=14, center_x=-110.5, center_y=-129.5)
        gx, gy = obs.to_grid_coords(origin_x=-280, origin_y=-341)
        assert abs(gx - 169.5) < 0.01
        assert abs(gy - 211.5) < 0.01


class TestParseObstacles:
    """Tests for _parse_obstacles function."""

    def test_parse_obstacles_list(self) -> None:
        """_parse_obstacles with bbp-decoded field 32 data returns correct list."""
        field32 = {
            "1": [
                {
                    "1": 1,
                    "2": 14,
                    "3": {"1": {"1": _float_to_uint32(-110.5), "2": _float_to_uint32(-129.5)}, "2": _float_to_uint32(11.0), "3": _float_to_uint32(41.0)},
                    "4": _float_to_uint32(180.0),
                },
                {
                    "1": 4,
                    "2": 2,
                    "3": {"1": {"1": _float_to_uint32(10.0), "2": _float_to_uint32(95.5)}, "2": _float_to_uint32(36.0), "3": _float_to_uint32(29.0)},
                    "4": _float_to_uint32(180.0),
                },
            ],
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 2
        assert obstacles[0].id == 1
        assert obstacles[0].type_id == 14
        assert obstacles[0].display_name == "Sofa"
        assert abs(obstacles[0].center_x - (-110.5)) < 0.5
        assert obstacles[1].id == 4
        assert obstacles[1].type_id == 2
        assert obstacles[1].display_name == "Double Bed"

    def test_parse_obstacles_empty_field32(self) -> None:
        """_parse_obstacles handles missing/empty field 32 gracefully."""
        assert _parse_obstacles({}) == []
        assert _parse_obstacles({"1": []}) == []

    def test_parse_obstacles_single_item_dict(self) -> None:
        """_parse_obstacles handles single item (dict not list) in field 32.1."""
        field32 = {
            "1": {
                "1": 13,
                "2": 4,
                "3": {"1": {"1": _float_to_uint32(-154.0), "2": _float_to_uint32(-55.5)}, "2": _float_to_uint32(13.0), "3": _float_to_uint32(20.0)},
                "4": _float_to_uint32(90.0),
            },
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert obstacles[0].id == 13
        assert obstacles[0].type_id == 4
        assert obstacles[0].display_name == "Dining Table"

    def test_parse_obstacles_float32_conversion(self) -> None:
        """float32 conversion works for coordinate values (uint32 bit patterns)."""
        # Use known value: -110.5 as uint32 = struct.unpack('I', struct.pack('f', -110.5))[0]
        field32 = {
            "1": {
                "1": 1,
                "2": 14,
                "3": {"1": {"1": _float_to_uint32(-110.5), "2": _float_to_uint32(-129.5)}, "2": _float_to_uint32(11.0), "3": _float_to_uint32(41.0)},
                "4": _float_to_uint32(180.0),
            },
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert abs(obstacles[0].center_x - (-110.5)) < 0.1
        assert abs(obstacles[0].center_y - (-129.5)) < 0.1
        assert abs(obstacles[0].width - 11.0) < 0.1
        assert abs(obstacles[0].height - 41.0) < 0.1
        assert abs(obstacles[0].angle - 180.0) < 0.1

    def test_parse_obstacles_skips_bad_items(self) -> None:
        """_parse_obstacles skips non-dict items without crashing."""
        field32 = {
            "1": [
                "not a dict",
                42,
                {"1": 1, "2": 28, "3": {"1": {"1": 0.0, "2": 0.0}}},
            ],
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert obstacles[0].type_id == 28

class TestCurrentRoomTracking:
    """Tests for current_room_id parsing and current_room_name lookup.

    working_status field 6 confirmed 2026-04-24 from live Flow 2 capture:
    value changed 4 (Corridor) → 1 (Living Room) as robot moved between rooms.
    """

    def test_current_room_id_from_working_status_field6(self) -> None:
        """Field 6 in working_status sets current_room_id."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4

    def test_current_room_id_updates_as_robot_moves(self) -> None:
        """current_room_id updates each time working_status arrives with field 6."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        state.update_from_working_status({"6": 1})
        assert state.current_room_id == 1

    def test_current_room_id_zero_becomes_none(self) -> None:
        """Field 6 = 0 is treated as absent (no room)."""
        state = NarwalState()
        state.update_from_working_status({"6": 0})
        assert state.current_room_id is None

    def test_current_room_id_not_cleared_when_field6_absent(self) -> None:
        """If field 6 is not in the message, current_room_id is not cleared.

        working_status messages without field 6 are routine (e.g. the idle
        heartbeat only sends a few fields). We must not reset current_room_id
        on every message — only update it when field 6 is explicitly present.
        """
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        # Message without field 6
        state.update_from_working_status({"3": 120, "13": 18000})
        assert state.current_room_id == 4  # unchanged

    def test_current_room_id_default_is_none(self) -> None:
        """Default state has no current room."""
        state = NarwalState()
        assert state.current_room_id is None

    def test_current_room_name_returns_none_when_no_current_room(self) -> None:
        """current_room_name is None when current_room_id is None."""
        state = NarwalState()
        assert state.current_room_name is None

    def test_current_room_name_returns_none_when_no_map(self) -> None:
        """current_room_name is None when map_data has not loaded yet."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.map_data is None
        assert state.current_room_name is None

    def test_current_room_name_with_user_named_room(self) -> None:
        """current_room_name returns user-assigned name for named rooms."""
        state = NarwalState()
        state.update_from_working_status({"6": 3})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, room_sub_type=3),     # Living Room
                RoomInfo(room_id=3, name="Phoebe's room"),  # user-named
            ],
        )
        assert state.current_room_name == "Phoebe's room"

    def test_current_room_name_with_type_named_room(self) -> None:
        """current_room_name falls back to room type name for unnamed rooms."""
        state = NarwalState()
        state.update_from_working_status({"6": 1})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, room_sub_type=3),  # type 3 = Living room
                RoomInfo(room_id=3, name="Phoebe's room"),
            ],
        )
        assert state.current_room_name == "Living room"

    def test_current_room_name_with_numbered_room(self) -> None:
        """current_room_name appends instance_index for duplicate room types."""
        state = NarwalState()
        state.update_from_working_status({"6": 10})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=7, room_sub_type=6, instance_index=1),   # Toilet
                RoomInfo(room_id=10, room_sub_type=6, instance_index=2),  # Toilet 2
                RoomInfo(room_id=11, room_sub_type=6, instance_index=3),  # Toilet 3
            ],
        )
        assert state.current_room_name == "Toilet 2"

    def test_current_room_name_unknown_room_id_returns_none(self) -> None:
        """current_room_name returns None if room_id not found in map."""
        state = NarwalState()
        state.update_from_working_status({"6": 99})
        state.map_data = MapData(
            rooms=[RoomInfo(room_id=1, room_sub_type=3)],
        )
        assert state.current_room_name is None

    def test_current_room_name_matches_live_capture(self) -> None:
        """Simulate the 2026-04-24 live capture: room 4 = Corridor, room 1 = Living room.

        Capture confirmed: field 6 changed from 4 to 1 as robot moved rooms.
        Names follow the shared RoomType table corrected in #48.
        """
        state = NarwalState()
        # Build room map from live get_map data
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, name="", room_sub_type=3),   # Living room (type 3)
                RoomInfo(room_id=4, name="", room_sub_type=10),  # Corridor (type 10)
            ],
        )
        # First capture: field 6 = 4 (Corridor)
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        assert state.current_room_name == "Corridor"

        # Second capture 22 minutes later: field 6 = 1 (Living room)
        state.update_from_working_status({"6": 1})
        assert state.current_room_id == 1
        assert state.current_room_name == "Living room"


class TestRoomInfoNames:
    """Tests for the shared RoomType→name table (issue #22).

    The app names rooms through one switch keyed only on the RoomType enum (no model parameter), so every model resolves the same names — taken verbatim from the app's en-US.json.
    """

    def test_shared_table_names(self) -> None:
        """Every RoomType resolves to its verbatim en-US.json name."""
        expected = {
            0: "Room", 1: "Master bedroom", 2: "Secondary bedroom",
            3: "Living room", 4: "Kitchen", 5: "Bathroom", 6: "Toilet",
            7: "Balcony", 8: "Dining room", 9: "Closet", 10: "Corridor",
            11: "Study", 12: "Kids' room", 13: "Entertainment room",
            14: "Storage room", 15: "Others",
        }
        for sub_type, name in expected.items():
            assert RoomInfo(room_sub_type=sub_type).display_name == name

    def test_user_assigned_name_wins(self) -> None:
        """A user-assigned name always wins over the table."""
        room = RoomInfo(room_sub_type=5, name="Powder Room")
        assert room.display_name == "Powder Room"

    def test_instance_index_appends(self) -> None:
        """Duplicate rooms get an instance-number suffix (Bathroom 2)."""
        room = RoomInfo(room_sub_type=5, instance_index=2)
        assert room.display_name == "Bathroom 2"

    def test_unknown_sub_type_falls_back_to_room(self) -> None:
        """An out-of-range sub-type falls back to the default name."""
        assert RoomInfo(room_sub_type=99).display_name == "Room"

    def test_map_data_from_response_resolves_names(self) -> None:
        """get_map parse resolves room names via the shared table."""
        decoded = {
            "2": {
                "12": [
                    {"1": 1, "2": 1, "3": b"", "4": 1, "8": 1},
                    {"1": 5, "2": 5, "3": b"", "4": 1, "8": 2},
                ],
            }
        }
        map_data = MapData.from_response(decoded)
        names = [r.display_name for r in map_data.rooms]
        assert names == ["Master bedroom", "Bathroom 2"]
