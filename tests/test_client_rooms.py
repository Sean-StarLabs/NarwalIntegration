"""Tests for the room-clean payload (_build_start_clean_payload) and start_rooms."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import blackboxprotobuf
import pytest

from narwal_client.client import NarwalClient, NarwalCommandError
from narwal_client.const import (
    CommandResult,
    CleaningRoute,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)
from narwal_client.models import CommandResponse


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _task(payload: bytes) -> dict:
    """Decode a StartClean payload to its CleanTask (field 1)."""
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded["1"]


def _items(task: dict) -> list[dict]:
    items = task["2"]
    return items if isinstance(items, list) else [items]


# WorkMode -> (expected taskType, expected CleanParam.mode, expected pass tags)
_MODE_EXPECT = {
    WorkMode.VACUUM: (1, 2, {"5"}),
    WorkMode.MOP: (2, 3, {"6"}),
    WorkMode.VACUUM_THEN_MOP: (3, 5, {"5", "6"}),
    WorkMode.VACUUM_AND_MOP: (4, 4, {"7"}),
}


class TestBuildStartCleanPayload:
    """The CleanTask/CleanParam encoding."""

    def test_legacy_fan_level_names_remain_available(self) -> None:
        """External client users keep the original QUIET and MAX names."""
        assert FanLevel.QUIET is FanLevel.MUTE
        assert FanLevel.MAX is FanLevel.DEEP

    def test_task_type_and_param_mode_per_mode(self) -> None:
        """taskType (the carrier) and CleanParam.mode/pass-tags follow work_mode."""
        client = NarwalClient("127.0.0.1")
        for mode, (task_type, param_mode, pass_tags) in _MODE_EXPECT.items():
            payload = client._build_start_clean_payload([2], 1, work_mode=mode, passes=2)
            task = _task(payload)
            assert task["5"] == task_type, f"taskType for {mode.name}"
            param = _items(task)[0]["2"]
            assert param["1"] == param_mode, f"CleanParam.mode for {mode.name}"
            for tag in pass_tags:
                assert param[tag] == 2, f"pass tag {tag} for {mode.name}"
            for tag in {"5", "6", "7"} - pass_tags:
                assert tag not in param, f"unexpected pass tag {tag} for {mode.name}"

    def test_fan_water_strength_encoded(self) -> None:
        """fan/water/mop_strength land in their CleanParam tags."""
        client = NarwalClient("127.0.0.1")
        payload = client._build_start_clean_payload(
            [2], 1, work_mode=WorkMode.MOP,
            fan=FanLevel.DEEP, water=MopHumidity.WET, mop_strength=MopStrengthLevel.HIGH,
        )
        param = _items(_task(payload))[0]["2"]
        assert param["2"] == FanLevel.DEEP
        assert param["4"] == MopHumidity.WET
        assert param["3"] == MopStrengthLevel.HIGH

    def test_overlap_not_sent(self) -> None:
        """overlapLevel (tag 8) is omitted — live-validated as ignored."""
        client = NarwalClient("127.0.0.1")
        param = _items(_task(client._build_start_clean_payload([2], 1)))[0]["2"]
        assert "8" not in param

    def test_route_encoded_when_requested(self) -> None:
        """Route overlap is encoded in CleanParam tag 8."""
        client = NarwalClient("127.0.0.1")
        payload = client._build_start_clean_payload(
            [2], 1, route=CleaningRoute.METICULOUS,
        )
        param = _items(_task(payload))[0]["2"]
        assert param["8"] == CleaningRoute.METICULOUS

    def test_map_zone_and_order(self) -> None:
        """map_id, room zone refs, and 1-based order encode correctly."""
        client = NarwalClient("127.0.0.1")
        task = _task(client._build_start_clean_payload([2, 12], 7))
        assert task["1"] == 7
        items = _items(task)
        assert [it["1"]["2"] for it in items] == [2, 12]
        assert all(it["1"]["1"] == 1 for it in items)  # zoneType = ROOM
        assert [it["3"] for it in items] == [1, 2]


class TestStartRooms:
    """start_rooms dispatch and settings threading."""

    def _client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client.state.device_info = MagicMock(product_key="QoEsI5qYXO")
        client.state.map_data = MagicMock(map_id=1)
        return client

    def test_empty_rooms_calls_start(self) -> None:
        """start_rooms([]) falls back to whole-house start()."""
        client = self._client()
        with patch.object(client, "start", new_callable=AsyncMock) as mock_start:
            _run(client.start_rooms([]))
            mock_start.assert_awaited_once()

    def test_forwards_settings_to_payload(self) -> None:
        """Settings passed to start_rooms reach the encoded CleanTask."""
        client = self._client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            _run(client.start_rooms(
                [5], work_mode=WorkMode.MOP, fan=FanLevel.STRONG,
                water=MopHumidity.DRY, mop_strength=MopStrengthLevel.HIGH, passes=3,
                route=CleaningRoute.METICULOUS,
            ))
        mock_send.assert_awaited_once()
        param = _items(_task(mock_send.await_args.kwargs["payload"]))[0]["2"]
        assert param["1"] == 3  # CleanParam.mode MOP
        assert param["2"] == FanLevel.STRONG
        assert param["3"] == MopStrengthLevel.HIGH
        assert param["4"] == MopHumidity.DRY
        assert param["6"] == 3  # mopTime pass count
        assert param["8"] == CleaningRoute.METICULOUS

    def test_defaults_match_documented_clean(self) -> None:
        """Default room cleaning uses max suction and wet mopping."""
        client = self._client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            _run(client.start_rooms([5]))

        param = _items(_task(mock_send.await_args.kwargs["payload"]))[0]["2"]
        assert param["2"] == FanLevel.DEEP
        assert param["4"] == MopHumidity.WET

    def test_no_map_id_uses_legacy_fallback(self) -> None:
        """No active map id skips start_clean but retains the legacy path."""
        client = self._client()
        client.state.device_info = MagicMock(product_key="QoEsI5qYXO")
        client.state.map_data = MagicMock(map_id=0)
        with patch.object(client, "get_map", new_callable=AsyncMock) as mock_get_map, \
             patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_get_map.return_value = MagicMock(map_id=0)
            mock_send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
            result = _run(client.start_rooms([5]))
        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == "clean/plan/start"
        assert result.result_code == CommandResult.SUCCESS

    def test_map_fetch_failure_uses_legacy_fallback(self) -> None:
        """A sleeping robot can still receive the legacy cached-room command."""
        client = self._client()
        client.state.device_info = MagicMock(product_key="QoEsI5qYXO")
        client.state.map_data = None
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(
            client,
            "get_map",
            new_callable=AsyncMock,
            side_effect=NarwalCommandError("timeout"),
        ), patch.object(
            client, "send_command", new_callable=AsyncMock, return_value=success
        ) as mock_send:
            result = _run(client.start_rooms([5]))

        assert result is success
        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == "clean/plan/start"

    def test_flow2_map_fetch_failure_does_not_start_legacy_clean(self) -> None:
        """A missing Flow 2 map cannot fall back to an unsafe whole-home clean."""
        client = self._client()
        client.state.device_info = MagicMock(product_key="QxMSPG6VSO")
        client.state.map_data = None
        with patch.object(
            client,
            "get_map",
            new_callable=AsyncMock,
            side_effect=NarwalCommandError("timeout"),
        ), patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            try:
                _run(client.start_rooms([5]))
            except NarwalCommandError:
                pass
            else:
                raise AssertionError("Flow 2 map failure should surface to the caller")

        mock_send.assert_not_awaited()

    def test_super_live_fan_uses_highest_supported_level(self) -> None:
        """The live command never reports an unavailable SUPER level."""
        client = self._client()
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            _run(client.set_fan_speed(FanLevel.SUPER))

        assert mock_send.await_args.args[0] == "clean/set_fan_level"
        assert mock_send.await_args.args[1] == b"\x08\x04"

    @pytest.mark.parametrize(
        ("legacy_level", "wire_level"),
        ((0, 1), (1, 2), (2, 3), (3, 4)),
    )
    def test_live_fan_preserves_legacy_integer_levels(
        self, legacy_level: int, wire_level: int
    ) -> None:
        client = self._client()
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            _run(client.set_fan_speed(legacy_level))

        assert mock_send.await_args.args[1] == bytes((0x08, wire_level))

    @pytest.mark.parametrize(
        ("legacy_level", "wire_level"),
        ((0, 1), (1, 2), (2, 3)),
    )
    def test_live_mop_preserves_legacy_integer_levels(
        self, legacy_level: int, wire_level: int
    ) -> None:
        client = self._client()
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            _run(client.set_mop_humidity(legacy_level))

        assert mock_send.await_args.args[1] == bytes((0x08, wire_level))

    def test_not_ready_retries_while_docked(self) -> None:
        """NOT_READY on the dock retries clean/start_clean (dock settling)."""
        client = self._client()
        client.state.update_from_base_status({"3": {"1": 10, "10": 1}})  # docked
        assert client.state.is_docked
        not_ready = CommandResponse(result_code=CommandResult.NOT_READY)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send, \
             patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock):
            mock_send.side_effect = [not_ready, success]
            result = _run(client.start_rooms([5]))
        assert mock_send.await_count == 2
        assert result is success

    def test_not_ready_off_dock_does_not_retry(self) -> None:
        """NOT_READY off the dock surfaces as-is — start_clean needs the dock."""
        client = self._client()
        assert not client.state.is_docked
        not_ready = CommandResponse(result_code=CommandResult.NOT_READY)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = not_ready
            result = _run(client.start_rooms([5]))
        mock_send.assert_awaited_once()
        assert result.result_code == CommandResult.NOT_READY

    def test_conflict_surfaces_without_retry(self) -> None:
        """A CONFLICT response (robot busy) is returned as-is."""
        client = self._client()
        conflict = CommandResponse(result_code=CommandResult.CONFLICT)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = conflict
            result = _run(client.start_rooms([5]))
        mock_send.assert_awaited_once()
        assert result.result_code == CommandResult.CONFLICT

    def test_not_applicable_falls_back_to_plan_start(self) -> None:
        """Older firmware retains the clean/plan/start compatibility path."""
        client = self._client()
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [not_applicable, success]
            result = _run(client.start_rooms([5]))

        assert result is success
        assert mock_send.await_count == 2
        assert mock_send.await_args_list[0].args[0] == "clean/start_clean"
        assert mock_send.await_args_list[1].args[0] == "clean/plan/start"

    def test_flow2_rejection_does_not_start_legacy_clean(self) -> None:
        """Flow 2 never falls back to plan/start whole-home behaviour."""
        client = self._client()
        client.state.device_info = MagicMock(product_key="QxMSPG6VSO")
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = not_applicable
            result = _run(client.start_rooms([5]))

        assert result is not_applicable
        mock_send.assert_awaited_once()

    def test_legacy_fallback_translates_water_level(self) -> None:
        """CleanParam wet maps to the legacy plan/start wet value."""
        client = self._client()
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [not_applicable, success]
            _run(client.start_rooms([5], water=MopHumidity.WET))

        legacy_payload = mock_send.await_args_list[1].kwargs["payload"]
        decoded, _ = blackboxprotobuf.decode_message(legacy_payload)
        assert decoded["1"]["2"]["2"]["7"] == 2

    def test_legacy_fallback_translates_fan_level(self) -> None:
        """Flow 2 suction levels stay within the legacy plan/start range."""
        client = self._client()
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [not_applicable, success]
            _run(client.start_rooms([5], fan=FanLevel.SUPER))

        legacy_payload = mock_send.await_args_list[1].kwargs["payload"]
        decoded, _ = blackboxprotobuf.decode_message(legacy_payload)
        assert decoded["1"]["2"]["2"]["1"] == 3

    def test_legacy_fallback_translates_strong_fan_level(self) -> None:
        """CleanParam strong maps to the legacy strong value."""
        client = self._client()
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [not_applicable, success]
            _run(client.start_rooms([5], fan=FanLevel.STRONG))

        legacy_payload = mock_send.await_args_list[1].kwargs["payload"]
        decoded, _ = blackboxprotobuf.decode_message(legacy_payload)
        assert decoded["1"]["2"]["2"]["1"] == 2

    def test_both_new_payloads_rejected_use_legacy_payload(self) -> None:
        """Older firmware receives the legacy flat-room payload last."""
        client = self._client()
        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [not_applicable, not_applicable, success]
            result = _run(client.start_rooms([5]))

        assert result is success
        assert mock_send.await_count == 3
        legacy_payload = mock_send.await_args_list[2].kwargs["payload"]
        decoded, _ = blackboxprotobuf.decode_message(legacy_payload)
        assert decoded["1"]["2"]["1"] == 5
