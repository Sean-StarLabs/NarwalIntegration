"""Tests for the room-clean payload (_build_start_clean_payload) and start_rooms."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import blackboxprotobuf
import pytest

from narwal_client.client import NarwalClient, NarwalCommandError, RoomCleanSettings
from narwal_client.const import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)
from narwal_client.models import CommandResponse


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

    def test_route_omitted_when_unset(self) -> None:
        """overlapLevel (tag 8) is omitted when no route is supplied."""
        client = NarwalClient("127.0.0.1")
        param = _items(_task(client._build_start_clean_payload([2], 1)))[0]["2"]
        assert "8" not in param

    def test_route_encoded_when_supplied(self) -> None:
        """Route lands in CleanParam overlapLevel tag 8."""
        client = NarwalClient("127.0.0.1")
        payload = client._build_start_clean_payload(
            [2], 1, route=CleaningRoute.METICULOUS
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

    def test_per_room_settings_encode_distinct_clean_params(self) -> None:
        """Each room can carry its own CleanParam in one CleanTask."""
        client = NarwalClient("127.0.0.1")
        payload = client._build_start_clean_payload(
            [2, 12],
            7,
            room_settings={
                2: RoomCleanSettings(
                    work_mode=WorkMode.VACUUM_THEN_MOP,
                    fan=FanLevel.UNSPECIFIED,
                    water=MopHumidity.WET,
                    mop_strength=MopStrengthLevel.HIGH,
                    passes=3,
                    route=CleaningRoute.METICULOUS,
                ),
                12: RoomCleanSettings(
                    work_mode=WorkMode.VACUUM_THEN_MOP,
                    fan=FanLevel.STRONG,
                    water=MopHumidity.NORMAL,
                    mop_strength=MopStrengthLevel.NORMAL,
                    passes=1,
                    route=CleaningRoute.STANDARD,
                ),
            },
        )

        task = _task(payload)
        assert task["5"] == WorkMode.VACUUM_THEN_MOP
        room_params = {item["1"]["2"]: item["2"] for item in _items(task)}
        assert room_params[2]["1"] == 5  # VACUUM_THEN_MOP CleanParam.mode
        assert room_params[2]["3"] == MopStrengthLevel.HIGH
        assert room_params[2]["4"] == MopHumidity.WET
        assert room_params[2]["5"] == 3
        assert room_params[2]["6"] == 3
        assert room_params[2]["8"] == CleaningRoute.METICULOUS

        assert room_params[12]["1"] == 5  # VACUUM_THEN_MOP CleanParam.mode
        assert room_params[12]["2"] == FanLevel.STRONG
        assert room_params[12]["4"] == MopHumidity.NORMAL
        assert room_params[12]["5"] == 1
        assert room_params[12]["6"] == 1
        assert room_params[12]["8"] == CleaningRoute.STANDARD

    def test_mixed_room_modes_are_rejected(self) -> None:
        """The outer CleanTask mode is not guessed for mixed per-room modes."""
        client = NarwalClient("127.0.0.1")

        with pytest.raises(ValueError, match="Mixed Narwal room clean modes"):
            client._build_start_clean_payload(
                [2, 12],
                7,
                room_settings={
                    2: RoomCleanSettings(work_mode=WorkMode.MOP),
                    12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
                },
            )


class TestStartRooms:
    """start_rooms dispatch and settings threading."""

    def _client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client.state.map_data = MagicMock(map_id=1)
        return client

    @pytest.mark.asyncio
    async def test_empty_rooms_return_not_ready_without_dispatch(self) -> None:
        """start_rooms([]) does not dispatch an ambiguous saved plan."""
        client = self._client()
        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            result = await client.start_rooms([])

        assert result.result_code == CommandResult.NOT_READY
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forwards_settings_to_payload(self) -> None:
        """Settings passed to start_rooms reach the encoded CleanTask."""
        client = self._client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            await client.start_rooms(
                [5], work_mode=WorkMode.MOP, fan=FanLevel.STRONG,
                water=MopHumidity.DRY, mop_strength=MopStrengthLevel.HIGH, passes=3,
                route=CleaningRoute.STANDARD,
            )
        mock_send.assert_awaited_once()
        param = _items(_task(mock_send.await_args.kwargs["payload"]))[0]["2"]
        assert param["1"] == 3  # CleanParam.mode MOP
        assert param["2"] == FanLevel.STRONG
        assert param["3"] == MopStrengthLevel.HIGH
        assert param["4"] == MopHumidity.DRY
        assert param["6"] == 3  # mopTime pass count
        assert param["8"] == CleaningRoute.STANDARD

    @pytest.mark.asyncio
    async def test_mixed_room_modes_return_not_applicable_without_sending(self) -> None:
        """Unsupported mixed-mode profiles fail before dispatching a command."""
        client = self._client()
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_rooms(
                [2, 12],
                room_settings={
                    2: RoomCleanSettings(work_mode=WorkMode.MOP),
                    12: RoomCleanSettings(work_mode=WorkMode.VACUUM),
                },
            )

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forwards_room_settings_to_payload(self) -> None:
        """Per-room settings passed to start_rooms reach the encoded CleanTask."""
        client = self._client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            await client.start_rooms(
                [5],
                room_settings={
                    5: RoomCleanSettings(
                        work_mode=WorkMode.MOP,
                        fan=FanLevel.UNSPECIFIED,
                        water=MopHumidity.WET,
                        mop_strength=MopStrengthLevel.HIGH,
                        passes=3,
                        route=CleaningRoute.METICULOUS,
                    )
                },
            )

        mock_send.assert_awaited_once()
        param = _items(_task(mock_send.await_args.kwargs["payload"]))[0]["2"]
        assert param["1"] == 3
        assert param["3"] == MopStrengthLevel.HIGH
        assert param["4"] == MopHumidity.WET
        assert param["6"] == 3
        assert param["8"] == CleaningRoute.METICULOUS

    @pytest.mark.asyncio
    async def test_no_map_id_returns_not_applicable(self) -> None:
        """No active map id (and none from get_map) → bail without sending."""
        client = self._client()
        client.state.map_data = MagicMock(map_id=0)
        with patch.object(client, "get_map", new_callable=AsyncMock) as mock_get_map, \
             patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_get_map.return_value = MagicMock(map_id=0)
            result = await client.start_rooms([5])
        mock_send.assert_not_awaited()
        assert result.result_code == CommandResult.NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_map_fetch_error_returns_not_applicable(self) -> None:
        """A failed map refresh bails out without sending a room-clean command."""
        client = self._client()
        client.state.map_data = None
        with patch.object(client, "get_map", new_callable=AsyncMock) as mock_get_map, \
             patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_get_map.side_effect = NarwalCommandError("no active map")
            result = await client.start_rooms([5])
        mock_get_map.assert_awaited_once()
        mock_send.assert_not_awaited()
        assert result.result_code == CommandResult.NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_not_ready_retries_while_docked(self) -> None:
        """NOT_READY on the dock retries clean/start_clean (dock settling)."""
        client = self._client()
        client.state.update_from_base_status({"3": {"1": 10, "10": 1}})  # docked
        assert client.state.is_docked
        not_ready = CommandResponse(result_code=CommandResult.NOT_READY)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send, \
             patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock):
            mock_send.side_effect = [not_ready, success]
            result = await client.start_rooms([5])
        assert mock_send.await_count == 2
        assert result is success

    @pytest.mark.asyncio
    async def test_not_ready_off_dock_does_not_retry(self) -> None:
        """NOT_READY off the dock surfaces as-is — start_clean needs the dock."""
        client = self._client()
        assert not client.state.is_docked
        not_ready = CommandResponse(result_code=CommandResult.NOT_READY)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = not_ready
            result = await client.start_rooms([5])
        mock_send.assert_awaited_once()
        assert result.result_code == CommandResult.NOT_READY

    @pytest.mark.asyncio
    async def test_conflict_surfaces_without_retry(self) -> None:
        """A CONFLICT response (robot busy) is returned as-is."""
        client = self._client()
        conflict = CommandResponse(result_code=CommandResult.CONFLICT)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = conflict
            result = await client.start_rooms([5])
        mock_send.assert_awaited_once()
        assert result.result_code == CommandResult.CONFLICT
