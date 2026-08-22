"""Tests for narwal_client.client — WebSocket client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from narwal_client.client import NarwalClient, NarwalConnectionError
from narwal_client.const import (
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_PLAN_START,
    TOPIC_CMD_RESET_CONSUMABLE_INFO,
    AmbientLightCtrlType,
    CommandResult,
    FanLevel,
    WorkingStatus,
)
from narwal_client.models import CommandResponse, MapData, RoomInfo


class TestNarwalClientInit:
    """Tests for NarwalClient initialization."""

    def test_default_port(self) -> None:
        client = NarwalClient("192.168.1.100")
        assert client.host == "192.168.1.100"
        assert client.port == 9002
        assert client.url == "ws://192.168.1.100:9002"

    def test_custom_port(self) -> None:
        client = NarwalClient("10.0.0.1", port=8080)
        assert client.port == 8080
        assert client.url == "ws://10.0.0.1:8080"

    def test_initial_state(self) -> None:
        client = NarwalClient("10.0.0.1")
        assert not client.connected
        assert client.state.battery_level == 0

    def test_unconfirmed_idle_base_status_preserves_active_metrics(self) -> None:
        """Stale idle base_status must not hide a fresh working_status task."""
        client = NarwalClient("10.0.0.1")
        client._update_from_working_status_broadcast({"3": 120})

        client._update_from_base_status_broadcast(
            {"3": {"1": 1}, "11": 1, "47": 2, "2": 87.0}
        )

        assert client.state.working_status == WorkingStatus.CLEANING
        assert client.state.battery_level == 87
        assert client.state.is_cleaning

    def test_confirmed_dock_base_status_ends_active_metrics(self) -> None:
        """Fresh dock indicators are trusted even after active task metrics."""
        client = NarwalClient("10.0.0.1")
        client._update_from_working_status_broadcast({"3": 120})

        client._update_from_base_status_broadcast(
            {"3": {"1": 10}, "11": 2, "47": 3}
        )

        assert client.state.working_status == WorkingStatus.DOCKED
        assert not client.state.has_recent_active_working_status
        assert client.state.is_docked

    @pytest.mark.asyncio
    async def test_commands_require_connection(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.start()

    @pytest.mark.asyncio
    async def test_send_raw_without_connection_raises(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.send_raw("test/topic", b"\x08\x01")

    @pytest.mark.asyncio
    async def test_set_ambient_light_mode_accepts_applied(self) -> None:
        """Dock light command accepts the observed applied result code."""
        client = NarwalClient("10.0.0.1")
        applied = CommandResponse(result_code=CommandResult.APPLIED)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = applied
            result = await client.set_ambient_light_mode(
                AmbientLightCtrlType.NIGHT_LIGHT
            )

        assert result is applied
        mock_send.assert_awaited_once_with(
            "supply/ambient_light_ctrl",
            payload=b"\x08\x01",
        )

    @pytest.mark.asyncio
    async def test_device_info_updates_firmware_state(self) -> None:
        """Device identity queries populate the firmware sensor state."""
        client = NarwalClient("10.0.0.1")
        response = CommandResponse(
            data={
                "1": b"hEA7OEshlx",
                "2": b"0123456789abcdef0123456789abcdef",
                "3": b"v01.13.11.02",
            }
        )

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = response
            info = await client.get_device_info()

        assert info.firmware_version == "v01.13.11.02"
        assert client.state.firmware_version == "v01.13.11.02"

    def test_addressed_response_marks_non_broadcast_model_reachable(self) -> None:
        """Non-broadcast models use command responses as reachability evidence."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)
        assert not client.robot_awake

        client._mark_response_received()

        assert client.robot_awake
        assert client._last_response_time > 0

    @pytest.mark.asyncio
    async def test_non_broadcast_wake_returns_without_waiting(self) -> None:
        """A connected non-broadcast model does not wait for impossible broadcasts."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)
        client._ws = AsyncMock()
        client._connected.set()

        with patch.object(client, "_send_wake_burst", new_callable=AsyncMock) as wake_burst:
            assert await client.wake(timeout=10.0)

        wake_burst.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_uses_plain_force_end_and_clears_timer(self) -> None:
        """Dock stop uses force_end, then refreshes stale mop drying state."""
        client = NarwalClient("10.0.0.1")
        client.state.dock_activity = 4
        client.state.dry_mop_remaining_time = 600

        async def refresh_timer() -> CommandResponse:
            client.state.clear_drying_task()
            return CommandResponse(result_code=0)

        with (
            patch.object(client, "send_command", new_callable=AsyncMock) as mock_send,
            patch.object(
                client, "get_dry_mop_remain_time", new_callable=AsyncMock
            ) as mock_refresh,
            patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
            mock_refresh.side_effect = refresh_timer

            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.SUCCESS
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            timeout=15.0,
        )
        mock_refresh.assert_awaited_once_with()
        assert not client.state.is_station_active

    @pytest.mark.asyncio
    async def test_stop_dock_task_clears_stale_washing_activity(self) -> None:
        """Dock stop clears old mop-washing flags after the robot accepts it."""
        client = NarwalClient("10.0.0.1")
        client.state.dock_activity = 3
        client.state.station_activity = 3

        with (
            patch.object(client, "send_command", new_callable=AsyncMock) as mock_send,
            patch.object(
                client,
                "get_dry_mop_remain_time",
                new_callable=AsyncMock,
                return_value=CommandResponse(result_code=CommandResult.SUCCESS),
            ),
            patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)

            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.SUCCESS
        assert client.state.dock_activity == 0
        assert client.state.station_activity == 0
        assert not client.state.is_washing_mop
        assert not client.state.is_station_active


class TestConsumableInfoReset:
    """Tests for local consumable-info reset payloads."""

    def test_reset_payload_encodes_maintain_and_replace_lists(self) -> None:
        client = NarwalClient("127.0.0.1")

        assert client._build_reset_consumable_info_payload(
            maintain_items=(4, 6, 8, 10),
            replace_items=(3, 20),
        ) == b"\x0a\x0a\x0a\x04\x04\x06\x08\x0a\x12\x02\x03\x14"

    def test_reset_payload_is_empty_without_targets(self) -> None:
        client = NarwalClient("127.0.0.1")

        assert client._build_reset_consumable_info_payload() == b""

    @pytest.mark.asyncio
    async def test_reset_does_not_fallback_to_empty_payload_when_target_stays_reported(
        self,
    ) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.maintain_items = [4]

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = [
                CommandResponse(result_code=CommandResult.SUCCESS),
                CommandResponse(data={"1": {"1": [4]}}),
            ]

            await client.reset_consumable_info(maintain_items=(4,))

        assert mock_send.await_args_list[0].args[:2] == (
            TOPIC_CMD_RESET_CONSUMABLE_INFO,
            b"\x0a\x03\x0a\x01\x04",
        )
        assert len(mock_send.await_args_list) == 2
        assert client.state.maintain_items == [4]

    @pytest.mark.asyncio
    async def test_reset_failure_does_not_fallback_to_empty_payload(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.maintain_items = [4, 6]

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(result_code=CommandResult.NOT_READY)

            await client.reset_consumable_info(maintain_items=(4,))

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[:2] == (
            TOPIC_CMD_RESET_CONSUMABLE_INFO,
            b"\x0a\x03\x0a\x01\x04",
        )
        assert client.state.maintain_items == [4, 6]


class TestBuildCleanPayloadV2:
    """Tests for the v2 clean payload schema introduced for firmware
    v01.07.22+ (issue #36)."""

    def test_single_room_uses_nested_room_id(self) -> None:
        """Single room encodes as a nested {1:1, 2:room_id} message."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([7])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        outer = decoded["1"]
        assert outer["1"] == 1
        assert outer["5"] == 6  # observed task source marker

        entry = outer["2"]
        assert entry["1"]["2"] == 7, "Room ID lives at 1.2.1.2 in v2 schema"
        assert entry["1"]["1"] == 1, "Inner field 1.2.1.1 was 1 in observed capture"
        assert entry["3"] == 1, "Sequence index starts at 1"

    def test_multiple_rooms_get_sequence_indices(self) -> None:
        """Multiple rooms preserve order via the 3:<seq> field, 1-indexed."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([5, 9, 3])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        entries = decoded["1"]["2"]
        assert isinstance(entries, list)
        assert len(entries) == 3
        assert [e["1"]["2"] for e in entries] == [5, 9, 3]
        assert [e["3"] for e in entries] == [1, 2, 3]

    def test_default_clean_params(self) -> None:
        """Default suction=3 / mop=2 / passes=1 / cleanMode=3 — Flow 1 max."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([1])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        params = decoded["1"]["2"]["2"]
        assert params["1"] == 3, "suction default 3 (Flow 1 max)"
        assert params["2"] == 3, "cleanMode default 3 (sweep+mop in v2)"
        assert params["3"] == 1, "passes default 1"
        assert params["7"] == 2, "mop_humidity default 2 (wet)"

    def test_custom_params_propagate(self) -> None:
        """Caller-provided params (suction=4 for Flow 2) reach the wire."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2(
            [1], suction=4, mop_humidity=1, passes=2, clean_mode=3
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        params = decoded["1"]["2"]["2"]
        assert params["1"] == 4
        assert params["7"] == 1
        assert params["3"] == 2

    def test_v2_payload_differs_from_legacy_default(self) -> None:
        """v2 schema must not collide with the legacy hardcoded default."""
        client = NarwalClient("127.0.0.1")
        v2 = client._build_clean_payload_v2([1])
        assert v2 != client._DEFAULT_CLEAN_PAYLOAD


class TestWholeHouseStart:
    """start() must route a whole-house clean through clean/start_clean (#69).

    The old implementation sent a minimal payload to clean/plan/start and
    returned on any result other than NOT_APPLICABLE. Newer firmware answers
    SUCCESS there and does nothing, so callers saw a start that never happened.
    """

    def _connected_client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client._connected = True
        return client

    @pytest.mark.asyncio
    async def test_start_uses_start_clean_with_every_room(self) -> None:
        """Cached map rooms are all enumerated onto clean/start_clean."""
        client = self._connected_client()
        client.state.map_data = MapData(
            map_id=1, rooms=[RoomInfo(room_id=11), RoomInfo(room_id=14)]
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = success
            result = await client.start()

        assert result is success
        topic = mock_send.await_args_list[0].args[0]
        assert topic == TOPIC_CMD_CLEAN_TASK
        assert topic != TOPIC_CMD_PLAN_START

    @pytest.mark.asyncio
    async def test_start_never_returns_early_on_plan_start_success(self) -> None:
        """Regression guard for #69: no path returns a bare clean/plan/start ack."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=3)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            await client.start()

        sent_topics = [call.args[0] for call in mock_send.await_args_list]
        assert TOPIC_CMD_PLAN_START not in sent_topics

    @pytest.mark.asyncio
    async def test_start_forwards_clean_settings(self) -> None:
        """Clean params reach the CleanTask payload rather than being dropped."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=6)])

        with patch.object(
            client, "start_rooms", new_callable=AsyncMock
        ) as mock_rooms:
            mock_rooms.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            await client.start(fan=FanLevel.STRONG, passes=2)

        assert mock_rooms.await_args.args[0] == [6]
        assert mock_rooms.await_args.kwargs["fan"] is FanLevel.STRONG
        assert mock_rooms.await_args.kwargs["passes"] == 2

    @pytest.mark.asyncio
    async def test_start_falls_back_to_saved_plan_without_rooms(self) -> None:
        """No map rooms — fall back to clean/plan/start as a last resort."""
        client = self._connected_client()
        assert client.state.map_data is None
        plan_resp = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_map", new_callable=AsyncMock
        ) as mock_map, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_map.side_effect = RuntimeError("no map")
            mock_send.return_value = plan_resp
            result = await client.start()

        assert result is plan_resp
        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == TOPIC_CMD_PLAN_START

    @pytest.mark.asyncio
    async def test_start_ignores_zero_room_ids(self) -> None:
        """A map whose rooms all have id 0 is treated as having no rooms."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=0)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            await client.start()

        assert mock_send.await_args.args[0] == TOPIC_CMD_PLAN_START

    @pytest.mark.asyncio
    async def test_start_rooms_with_empty_list_does_not_recurse(self) -> None:
        """start_rooms([]) must not bounce back into start() and loop."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            result = await client.start_rooms([])

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == TOPIC_CMD_PLAN_START
        assert result.result_code == CommandResult.SUCCESS
