"""Tests for narwal_client.models — state data models."""

from __future__ import annotations

import struct
import time
import zlib

from narwal_client.const import WorkingStatus
from narwal_client.models import (
    MapData,
    NarwalState,
    ObstacleInfo,
    RoomInfo,
    _parse_obstacles,
)


def test_map_data_preserves_existing_positional_arguments() -> None:
    room = RoomInfo(room_id=7)
    obstacle = ObstacleInfo(id=1)

    map_data = MapData(
        10,
        20,
        5,
        [room],
        b"map",
        200,
        1234,
        1.5,
        2.5,
        3,
        4,
        [obstacle],
        {"source": "test"},
    )

    assert map_data.width == 10
    assert map_data.height == 20
    assert map_data.rooms == [room]
    assert map_data.raw == {"source": "test"}
    assert map_data.map_id == 0


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

    def test_update_from_working_status(self) -> None:
        """working_status topic sets cleaning metrics and infers active cleaning."""
        state = NarwalState()
        state.update_from_working_status({"3": 120, "13": 18000, "15": 600})
        assert state.cleaning_time == 120
        assert state.cleaning_area == 18000
        assert state.working_status == WorkingStatus.CLEANING

    def test_working_status_clears_stale_station_activity(self) -> None:
        """A fresh clean must override stale dock-side activity."""
        state = NarwalState(
            working_status=WorkingStatus.DOCKED,
            dock_sub_state=1,
            station_activity=4,
        )

        state.update_from_working_status({"3": 120, "13": 18000})

        assert state.working_status == WorkingStatus.CLEANING
        assert state.station_activity == 0
        assert state.is_cleaning
        assert not state.is_docked

    def test_update_from_base_status_cleaning(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(85.0)})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_cleaning
        assert state.battery_level == 85

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
        assert state.battery_health == 100

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

    def test_update_from_base_status_station_activity(self) -> None:
        """field3.18 means station-side work is active."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 19, "18": 4}})
        assert state.station_activity == 4
        assert state.is_station_active
        assert state.is_docked

    def test_station_activity_resets_when_absent(self) -> None:
        """field3.18 resets when later base status omits it."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 19, "18": 4}})
        state.update_from_base_status({"3": {"1": 1, "3": 1}})
        assert state.station_activity == 0
        assert not state.is_station_active

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

    def test_new_fw_field3_unknown_subfields_logged(self) -> None:
        """New firmware sub-fields (4, 11) are parsed without error."""
        state = NarwalState()
        # Should not raise — unknown sub-fields logged at debug level
        state.update_from_base_status({"3": {"1": 2, "4": 99, "11": 3}})
        assert state.working_status == WorkingStatus.DOCKED_V2

    def test_off_dock_fields_clear_stale_dock_subfields(self) -> None:
        """Flow 2 off-dock fields override dock subfields omitted by new firmware."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 10, "10": 1, "12": 2}, "11": 2, "47": 3}
        )
        assert state.is_docked

        state.update_from_base_status(
            {"3": {"1": 3, "4": 7}, "11": 1, "47": 2}
        )

        assert state.dock_sub_state == 0
        assert state.dock_activity == 0
        assert state.is_cleaning
        assert not state.is_docked

    def test_active_status_clears_stale_dock_subfields(self) -> None:
        """An active status clears dock fields omitted by Flow 2 firmware."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 10, "10": 1, "12": 2}, "11": 2, "47": 3}
        )

        state.update_from_base_status({"3": {"1": 3, "4": 7}})

        assert state.dock_sub_state == 0
        assert state.dock_activity == 0
        assert state.is_cleaning
        assert not state.is_docked

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
        state.update_from_base_status({"3": {"1": 99}})
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
        assert state.battery_health == 100
        assert state.timestamp == 1757252225
        assert state.session_id == "d4bec8c82c484a3ba0428bb0dd4359e2"

    def test_update_from_upgrade_status(self) -> None:
        state = NarwalState()
        state.update_from_upgrade_status({
            "7": "v01.02.19.02",
            "8": "v01.02.19.02",
            "4": 10,
        })
        assert state.firmware_version == "v01.02.19.02"
        assert state.firmware_target == "v01.02.19.02"
        assert state.upgrade_status_code == 10

    def test_update_from_download_status(self) -> None:
        state = NarwalState()
        state.update_from_download_status({"1": 2})
        assert state.download_status == 2

    def test_update_from_robot_task_status(self) -> None:
        """Polled and broadcast task status expose progress and room."""
        for topic in ("robot/task/status/get", "robot/task/status"):
            state = NarwalState()
            state.update_from_aux_status(
                topic,
                {
                    "1": 1,
                    "2": {
                        "1": 93,
                        "2": 694,
                        "6": {"1": 5, "2": 1, "3": "Landing"},
                    },
                },
            )
            assert state.task_progress_percent == 93
            assert state.task_elapsed_time == 694
            assert state.current_room_id == 5
            assert state.current_room_name == "Landing"

    def test_update_from_robot_task_status_decodes_room_name(self) -> None:
        """Task room names are decoded from protobuf bytes."""
        state = NarwalState()

        state.update_from_aux_status(
            "robot/task/status",
            {"2": {"1": 42, "6": {"1": 5, "3": b"Landing"}}},
        )

        assert state.current_room_name == "Landing"

    def test_task_room_uses_current_map_display_name(self) -> None:
        """Task telemetry uses the current user-facing map room name."""
        state = NarwalState(
            map_data=MapData(rooms=[RoomInfo(room_id=6, name="Bathroom")])
        )

        state.update_from_aux_status(
            "robot/task/status",
            {"2": {"1": 42, "6": {"1": 6, "3": "浴室"}}},
        )

        assert state.current_room_name == "Bathroom"

    def test_incremental_updates(self) -> None:
        """State should accumulate across multiple topic updates."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(95.0)})
        state.update_from_working_status({"3": 120, "13": 18000})
        state.update_from_upgrade_status({"7": "v01.02.19.02"})

        assert state.battery_level == 95
        assert state.is_cleaning
        assert state.cleaning_time == 120
        assert state.cleaning_area == 18000
        assert state.firmware_version == "v01.02.19.02"

    def test_raw_data_preserved(self) -> None:
        state = NarwalState()
        raw = {"2": _float_to_uint32(100.0), "38": 100, "47": 2, "unknown_field": "value"}
        state.update_from_base_status(raw)
        assert state.raw_base_status == raw

    def test_aux_status_preserved(self) -> None:
        state = NarwalState()
        raw = {"1": {"2": 40}, "3": 120}
        state.update_from_aux_status("status/robot", raw)
        assert state.raw_aux_status["status/robot"] == raw

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

    def test_battery_health_field38_static(self) -> None:
        """Field 38 is static battery health (always 100), not real-time SOC."""
        state = NarwalState()
        state.update_from_base_status({"38": 100})
        assert state.battery_health == 100
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
        assert state.battery_health == 100
        assert state.working_status == WorkingStatus.DOCKED  # NOT overwritten
        assert state.is_docked  # still correct

    def test_repeated_stale_working_status_does_not_refresh_activity(self) -> None:
        """Unchanged working_status counters are stale, not active cleaning."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 4},
            "11": 1,
            "47": 2,
            "2": _float_to_uint32(80.0),
        })
        state.update_from_working_status({"3": 120, "13": 18000})
        assert state.has_recent_active_working_status

        state.last_active_working_status_time = time.monotonic() - 20
        state.update_from_working_status({"3": 120, "13": 18000})

        assert not state.has_recent_active_working_status

    def test_explicit_docked_status_ends_recent_activity(self) -> None:
        """A terminal base status wins immediately over recent task counters."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "11": 1, "47": 2})
        state.update_from_working_status({"3": 120, "13": 18000})
        assert state.has_recent_active_working_status

        state.update_from_base_status({"3": {"1": 10}, "11": 2, "47": 3})

        assert not state.has_recent_active_working_status
        assert state.is_docked
        assert not state.is_cleaning

    def test_rising_battery_infers_stale_cleaning_is_docked(self) -> None:
        """A rising battery after stale cleaning telemetry means charging/docked."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 4},
            "11": 1,
            "47": 2,
            "2": _float_to_uint32(80.0),
        })
        state.update_from_working_status({"3": 120, "13": 18000})
        state.last_active_working_status_time = time.monotonic() - 20

        state.update_battery_from_base_status({"2": _float_to_uint32(81.0)})

        assert state.inferred_docked_from_battery
        assert state.is_docked
        assert not state.is_cleaning

    def test_explicit_off_dock_status_clears_inferred_docked_state(self) -> None:
        """Fresh off-dock fields override battery-based dock inference."""
        state = NarwalState(inferred_docked_from_battery=True)

        state.update_from_base_status({"3": {"1": 4}, "11": 1, "47": 2})

        assert not state.inferred_docked_from_battery
        assert not state.is_docked

    def test_explicit_off_dock_status_wins_over_rising_battery(self) -> None:
        """Battery inference cannot override off-dock fields in the same packet."""
        state = NarwalState(
            working_status=WorkingStatus.CLEANING,
            battery_level=80,
        )

        state.update_from_base_status(
            {"3": {"1": 4}, "11": 1, "47": 2, "2": _float_to_uint32(81.0)}
        )

        assert not state.inferred_docked_from_battery
        assert not state.is_docked

    def test_dock_fields_override_stale_task_completed_status(self) -> None:
        """Flow 2 can report TASK_COMPLETED while dock fields already say docked."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 4},
            "11": 1,
            "47": 2,
            "2": _float_to_uint32(80.0),
        })
        state.update_from_working_status({"3": 120, "13": 18000})
        state.last_active_working_status_time = time.monotonic() - 20

        state.update_from_base_status({
            "3": {"1": 19, "18": 4},
            "11": 2,
            "47": 3,
            "2": _float_to_uint32(82.0),
        })

        assert state.working_status == WorkingStatus.TASK_COMPLETED
        assert state.is_docked
        assert not state.is_cleaning

    def test_returning_to_dock_field7(self) -> None:
        """Field 3.7=1 indicates returning to dock (confirmed live)."""
        state = NarwalState()
        # Live data: {1=4, 7=1, 10=2} — CLEANING + returning + docking
        state.update_from_base_status(
            {"3": {"1": 4, "7": 1, "10": 2}, "11": 1, "47": 2}
        )
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

    def test_unknown_working_status_value(self) -> None:
        """Unknown status values should fall back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 255}})
        assert state.working_status == WorkingStatus.UNKNOWN


def _float_to_uint32(f: float) -> int:
    """Encode a float as the uint32 bit pattern (for protobuf simulation)."""
    return struct.unpack("I", struct.pack("f", f))[0]


class TestMapData:
    """Tests for MapData.from_response()."""

    def test_unassigned_obstacles_are_not_cleanable_floor(self) -> None:
        """Whole-map estimates exclude unassigned obstacle pixels."""
        map_data = MapData(
            width=2,
            height=1,
            resolution=10,
            compressed_map=zlib.compress(bytes([0x0A, 0x02, 0x20, 0x28])),
        )

        assert map_data.cleanable_area_cm2() == 1

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
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "2": 152, "3": -280, "4": 60},
            "8": {"1": {"1": _float_to_uint32(-8.0188), "2": _float_to_uint32(0.221)}, "2": _float_to_uint32(0.036)},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        # factor 1.0: -8.0188 - (-280) = 271.98, 0.221 - (-341) = 341.22
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 272.0) < 1.0
        assert abs(m.dock_y - 341.2) < 1.0

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
        # factor 1.0: -8.0188 - (-280) = 271.98, 0.221 - (-341) = 341.22
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 272.0) < 1.0
        assert abs(m.dock_y - 341.2) < 1.0

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




class TestRoomInfoModelOverrides:
    """Tests for per-model ROOM_TYPE_NAMES overrides (issue #22)."""

    def test_flow_1_uses_default_names(self) -> None:
        """Flow 1 (no/empty model_key) keeps the original sub-type names."""
        room = RoomInfo(room_id=1, room_sub_type=1)
        assert room.display_name == "Primary Bedroom"
        room = RoomInfo(room_id=5, room_sub_type=5)
        assert room.display_name == "Study"
        room = RoomInfo(room_id=10, room_sub_type=10)
        assert room.display_name == "Utility Room"

    def test_flow_2_overrides_apply(self) -> None:
        """Flow 2 product key renames sub-types 1, 5, 10."""
        for flow2 in ("QxMSPG6VSO", "iSuVlI1If2"):
            assert (
                RoomInfo(room_sub_type=1, model_key=flow2).display_name
                == "Master Bedroom"
            )
            assert RoomInfo(room_sub_type=5, model_key=flow2).display_name == "Bathroom"
            assert RoomInfo(room_sub_type=10, model_key=flow2).display_name == "Corridor"

    def test_flow_2_non_overridden_types_use_defaults(self) -> None:
        """Sub-types not in the Flow 2 override map use the base names."""
        flow2 = "QxMSPG6VSO"
        assert RoomInfo(room_sub_type=3, model_key=flow2).display_name == "Living Room"
        assert RoomInfo(room_sub_type=4, model_key=flow2).display_name == "Kitchen"
        assert RoomInfo(room_sub_type=6, model_key=flow2).display_name == "Bathroom"

    def test_user_assigned_name_wins_over_override(self) -> None:
        """A user-assigned name always wins, regardless of model overrides."""
        room = RoomInfo(
            room_sub_type=5, model_key="QxMSPG6VSO", name="Powder Room"
        )
        assert room.display_name == "Powder Room"

    def test_instance_index_appends_to_overridden_name(self) -> None:
        """Duplicate Flow 2 bathrooms number as Bathroom 2, 3..."""
        room = RoomInfo(
            room_sub_type=5, model_key="QxMSPG6VSO", instance_index=2
        )
        assert room.display_name == "Bathroom 2"

    def test_map_data_from_response_propagates_product_key(self) -> None:
        """get_map parse pushes product_key into every RoomInfo."""
        decoded = {
            "2": {
                "12": [
                    {"1": 1, "2": 1, "3": b"", "4": 1, "8": 1},
                    {"1": 5, "2": 5, "3": b"", "4": 1, "8": 2},
                ],
            }
        }
        map_data = MapData.from_response(decoded, product_key="QxMSPG6VSO")
        names = [r.display_name for r in map_data.rooms]
        assert names == ["Master Bedroom", "Bathroom 2"]

    def test_map_data_from_response_default_no_key(self) -> None:
        """Omitting product_key keeps the original behavior unchanged."""
        decoded = {
            "2": {"12": [{"1": 1, "2": 1, "3": b"", "4": 1, "8": 1}]},
        }
        map_data = MapData.from_response(decoded)
        assert map_data.rooms[0].display_name == "Primary Bedroom"
