"""Tests for narwal_client.client — WebSocket client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from narwal_client.client import NarwalClient, NarwalConnectionError
from narwal_client.const import (
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_DRY_DUST_BAG,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DRY_STATION_BAG,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_PLAN_START,
    TOPIC_CMD_WASH_MOP,
    AmbientLightCtrlType,
    CommandResult,
    FanLevel,
    WorkingStatus,
)
from narwal_client.models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    CommandResponse,
    MapData,
    RoomInfo,
)


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

    def test_command_response_accepted_codes(self) -> None:
        """Narwal uses code 0 for accepted async commands and 1 for success."""
        accepted = CommandResponse(result_code=0)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        applied = CommandResponse(result_code=CommandResult.APPLIED)
        rejected = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

        assert accepted.accepted
        assert not accepted.success
        assert success.accepted
        assert success.success
        assert applied.accepted
        assert not rejected.accepted

    @pytest.mark.asyncio
    async def test_get_status_without_base_payload_returns_not_ready(self) -> None:
        """A get_status ack without robot_base_status field 2 is not a refresh."""
        client = NarwalClient("10.0.0.1")
        ack_without_status = CommandResponse(
            result_code=CommandResult.SUCCESS,
            data={"1": 1},
            raw_payload=b"raw",
        )

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = ack_without_status
            result = await client.get_status()

        assert result.result_code == CommandResult.NOT_READY
        assert result.data == {"1": 1}
        assert result.raw_payload == b"raw"
        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_BASE_STATUS)

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

    @pytest.mark.asyncio
    async def test_start_rooms_rejects_active_dock_task(self) -> None:
        """Direct room starts are blocked while incompatible dock work is active."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_rooms([2])

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_rooms_reserves_private_guard_on_acceptance(self) -> None:
        """Accepted direct room starts block follow-up starts until telemetry arrives."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            first = await client.start_rooms([2])
            second = await client.start_rooms([2])

        assert first is success
        assert second.result_code == CommandResult.NOT_APPLICABLE
        assert client.state.has_assumed_robot_clean
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_direct_start_rooms_serialize_preflight(self) -> None:
        """Only one direct room start validates before the accepted-command guard."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = slow_send
            first = asyncio.create_task(client.start_rooms([2]))
            await command_started.wait()
            second = asyncio.create_task(client.start_rooms([2]))
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_rejects_active_dock_task_before_map_fallback(self) -> None:
        """Whole-house start is blocked even when it would use the saved plan."""
        client = self._connected_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_easy_clean_rejects_active_dock_task(self) -> None:
        """Quick clean uses the same dock-task guard as other robot starts."""
        client = self._connected_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_easy_clean()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_easy_clean_reserves_private_guard_on_acceptance(self) -> None:
        """Accepted quick-clean starts use the same duplicate-start guard."""
        client = self._connected_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            first = await client.start_easy_clean()
            second = await client.start_easy_clean()

        assert first is success
        assert second.result_code == CommandResult.NOT_APPLICABLE
        assert client.state.has_assumed_robot_clean
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_rooms_allows_typed_dock_bag_drying(self) -> None:
        """Dock-bag drying is the one known dock task compatible with robot start."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            result = await client.start_rooms([2])

        assert result is success
        mock_send.assert_awaited_once()


class TestDockTaskCommands:
    """Dock task commands and scoped dock-task stop behavior."""

    def _docked_client(self) -> NarwalClient:
        """Return a client with explicit idle-docked robot state."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.DOCKED
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        return client

    def _docked_status_response(self) -> CommandResponse:
        """Return a valid docked base-status refresh response."""
        return CommandResponse(
            data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}},
        )

    @pytest.mark.asyncio
    async def test_empty_dustbin_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.empty_dustbin()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DUST_GATHERING)
        assert client.state.assumed_active_dock_task == DOCK_TASK_EMPTY_DUSTBIN

    @pytest.mark.asyncio
    async def test_wash_mop_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.wash_mop()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_WASH_MOP)
        assert client.state.assumed_active_dock_task == DOCK_TASK_WASH_MOP

    @pytest.mark.asyncio
    async def test_dry_mop_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_mop()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_MOP)
        assert client.state.assumed_active_dock_task == DOCK_TASK_DRY_MOP

    @pytest.mark.asyncio
    async def test_dry_dust_bag_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_dust_bag()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_DUST_BAG)
        assert client.state.assumed_active_dock_drying_task == DOCK_TASK_DRY_DUST_BIN

    @pytest.mark.asyncio
    async def test_dry_station_bag_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_station_bag()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_STATION_BAG)
        assert client.state.assumed_active_dock_drying_task == DOCK_TASK_DRY_DOCK_BAG

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_cleaning_state(self) -> None:
        """Direct client dock commands cannot bypass robot-work gating."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.empty_dustbin()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_existing_dock_task(self) -> None:
        """Only one unscoped dock start may be dispatched at a time."""
        client = self._docked_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.wash_mop_by_robot_status()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_private_command_guard(self) -> None:
        """Accepted-command guards serialize direct dock starts without publishing state."""
        client = self._docked_client()
        client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.wash_mop_by_robot_status()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_direct_dock_commands_serialize_preflight(self) -> None:
        """Only one direct dock start validates and reserves an idle snapshot."""
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}},
            )
            mock_send.side_effect = slow_send
            first = asyncio.create_task(client.dry_dust_bag())
            await command_started.wait()
            second = asyncio.create_task(client.dry_station_bag())
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        assert mock_status.await_count == 1
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_DUST_BAG)
        assert client.state.assumed_active_dock_task == DOCK_TASK_DRY_DUST_BIN

    @pytest.mark.asyncio
    async def test_direct_dock_command_refreshes_before_start(self) -> None:
        """Direct dock starts revalidate the device state inside the action lock."""
        client = self._docked_client()

        async def stale_refresh(*args, **kwargs):
            client.state.working_status = WorkingStatus.CLEANING
            return CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}},
            )

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.side_effect = stale_refresh
            result = await client.dry_dust_bag()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_battery_only_refresh(self) -> None:
        """Direct dock starts require live dock status, not only any status field."""
        client = self._docked_client()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = CommandResponse(data={"2": {"2": 85.0}})
            result = await client.empty_dustbin()

        assert result.result_code == CommandResult.NOT_READY
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_unscoped_clean_context(self) -> None:
        """Direct dock stops cannot use generic force-end during robot return."""
        client = self._docked_client()
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 1

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_uses_scoped_force_end_payload(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is not None

    @pytest.mark.asyncio
    async def test_stop_dry_dust_bag_rejects_generic_force_end(self) -> None:
        """Dry dust-bin force_end variants accept but do not stop the task."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        client.state.dock_presence = 1

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_DRY_DUST_BIN)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DUST_BIN) is not None

    @pytest.mark.asyncio
    async def test_stop_dry_dust_bag_rejects_unscoped_generic_stop(self) -> None:
        """Unscoped dock stop must not use generic stop for dry dust-bin drying."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        client.state.dock_presence = 1

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DUST_BIN) is not None

    @pytest.mark.asyncio
    async def test_stop_dock_task_keeps_timer_when_refresh_fails(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = False
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is not None

    @pytest.mark.asyncio
    async def test_stop_dock_task_clears_timer_after_confirmed_idle_refresh(self) -> None:
        """Accepted dock stop clears the stopped timer once fresh dock status is idle."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        async def refresh_idle() -> bool:
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED), "3": 6}, "11": 2}
            )
            return True

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.side_effect = refresh_idle
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is None

    @pytest.mark.asyncio
    async def test_concurrent_direct_dock_stops_serialize_preflight(self) -> None:
        """Only one direct dock stop validates an active task snapshot."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        async def refresh_state():
            client.state.clear_dock_drying_task(DOCK_TASK_DRY_DOCK_BAG)
            return True

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.side_effect = slow_send
            mock_refresh.side_effect = refresh_state
            first = asyncio.create_task(client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG))
            await command_started.wait()
            second = asyncio.create_task(client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG))
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        assert mock_status.await_count == 2

    @pytest.mark.asyncio
    async def test_refresh_after_dock_stop_rejects_missing_base_status_payload(self) -> None:
        client = NarwalClient("127.0.0.1")

        with patch.object(client, "get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = CommandResponse(
                result_code=CommandResult.NOT_READY,
                data={"1": 1},
            )
            assert not await client._refresh_after_dock_stop()

        mock_status.assert_awaited_once_with(full_update=True)

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_ambiguous_generic_stop(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_DRY_MOP)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_unmapped_activity(self) -> None:
        """Unknown active dock work is not safe to stop with the generic command."""
        client = NarwalClient("127.0.0.1")
        client.state.station_activity = 99

        with patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_stop.assert_not_awaited()
