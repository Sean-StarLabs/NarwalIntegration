"""Tests for narwal_client.client — WebSocket client."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from narwal_client.client import (
    NarwalClient,
    NarwalCommandError,
    NarwalConnectionError,
    _QueuedResponse,
)
from narwal_client.const import (
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_GET_CONSUMABLE_INFO,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_PLAN_START,
    TOPIC_CMD_TAKE_PICTURE,
    AmbientLightCtrlType,
    CommandResult,
    FanLevel,
    WorkingStatus,
)
from narwal_client.models import CommandResponse, MapData, RoomInfo
from narwal_client.protocol import PROTOBUF_FIELD5_TAG, NarwalMessage, build_frame, parse_frame


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
    async def test_unmatched_field5_does_not_mark_non_broadcast_model_awake(self) -> None:
        """Only routed responses count as reachability evidence."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)

        await client._handle_message(bytes([0x01, 0x02, PROTOBUF_FIELD5_TAG, 0x00]))

        assert not client.robot_awake
        assert client._last_response_time == 0

    @pytest.mark.asyncio
    async def test_non_broadcast_wake_returns_without_waiting(self) -> None:
        """A connected non-broadcast model does not wait for impossible broadcasts."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)
        client._ws = AsyncMock()
        client._connected.set()

        with patch.object(client, "_send_wake_burst", new_callable=AsyncMock) as wake_burst:
            assert await client.wake(timeout=10.0)

        wake_burst.assert_not_awaited()


class TestCommandResponseRouting:
    """Command responses must be matched to their command topic."""

    @staticmethod
    def _message(short_topic: str, payload: bytes = b"\x08\x01") -> NarwalMessage:
        full_topic = f"/mock/device/{short_topic}"
        return NarwalMessage(
            topic=full_topic,
            payload=payload,
            header_byte=len(full_topic) + 2,
            field_tag=PROTOBUF_FIELD5_TAG,
            raw=b"",
        )

    @staticmethod
    def _field5_frame(full_topic: str, payload: bytes = b"\x08\x01") -> bytes:
        frame = bytearray(build_frame(full_topic, payload))
        frame[2] = PROTOBUF_FIELD5_TAG
        return bytes(frame)

    @staticmethod
    def _field5_frame_with_empty_topic(payload: bytes = b"\x08\x01") -> bytes:
        return bytes([0x01, 0x02, PROTOBUF_FIELD5_TAG, 0x00]) + payload

    @staticmethod
    def _broadcast_frame(full_topic: str, payload: bytes = b"") -> bytes:
        return build_frame(full_topic, payload)

    @pytest.mark.asyncio
    async def test_listener_mode_uses_matching_response_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True
        await client._response_queue_for(TOPIC_CMD_GET_BASE_STATUS).put(
            _QueuedResponse(
                time.monotonic(),
                self._message(TOPIC_CMD_GET_BASE_STATUS, b"\x08\x03"),
            )
        )

        async def enqueue_expected() -> None:
            await asyncio.sleep(0)
            await client._response_queue_for(TOPIC_CMD_FORCE_END).put(
                _QueuedResponse(time.monotonic(), self._message(TOPIC_CMD_FORCE_END))
            )

        task = asyncio.create_task(enqueue_expected())
        result = await client.send_command(TOPIC_CMD_FORCE_END)
        await task

        assert result.success

    @pytest.mark.asyncio
    async def test_listener_mode_accepts_empty_response_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        async def enqueue_expected() -> None:
            await asyncio.sleep(0)
            await client._handle_message(self._field5_frame_with_empty_topic())

        task = asyncio.create_task(enqueue_expected())
        result = await client.send_command(TOPIC_CMD_FORCE_END)
        await task

        assert result.success

    @pytest.mark.asyncio
    async def test_listener_mode_rejects_empty_response_topic_for_data_query(self) -> None:
        """Delayed fire-and-forget ACKs must not satisfy query commands."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        async def enqueue_responses() -> None:
            await asyncio.sleep(0)
            await client._handle_message(self._field5_frame_with_empty_topic())
            await client._handle_message(
                self._field5_frame(client._full_topic(TOPIC_CMD_GET_CONSUMABLE_INFO))
            )

        task = asyncio.create_task(enqueue_responses())
        result = await client.send_command(TOPIC_CMD_GET_CONSUMABLE_INFO)
        await task

        assert result.success

    @pytest.mark.asyncio
    async def test_late_optional_topicless_ack_does_not_satisfy_next_command(self) -> None:
        """A delayed optional wake ACK must not complete a following user command."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        with patch("narwal_client.client._TOPICLESS_ACK_QUARANTINE_SECONDS", 0.01):
            assert not await client._send_optional_ack_command(
                TOPIC_CMD_NOTIFY_APP_EVENT,
                timeout=0.001,
                label="Wake burst",
            )

            async def enqueue_responses() -> None:
                await asyncio.sleep(0)
                await client._handle_message(self._field5_frame_with_empty_topic())
                await asyncio.sleep(0.02)
                await client._handle_message(
                    self._field5_frame(
                        client._full_topic(TOPIC_CMD_FORCE_END),
                        b"\x08\x03",
                    )
                )

            task = asyncio.create_task(enqueue_responses())
            result = await client.send_command(TOPIC_CMD_FORCE_END)
            await task

        assert result.result_code == CommandResult.CONFLICT

    @pytest.mark.asyncio
    async def test_late_user_action_topicless_ack_does_not_satisfy_next_command(self) -> None:
        """A timed-out user command's late ACK must not complete the next command."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        with patch("narwal_client.client._TOPICLESS_ACK_QUARANTINE_SECONDS", 0.01):
            with pytest.raises(NarwalCommandError):
                await client.send_command(TOPIC_CMD_FORCE_END, timeout=0.001)

            async def enqueue_responses() -> None:
                await asyncio.sleep(0)
                await client._handle_message(self._field5_frame_with_empty_topic())
                await asyncio.sleep(0.02)
                await client._handle_message(
                    self._field5_frame(
                        client._full_topic(TOPIC_CMD_FORCE_END),
                        b"\x08\x03",
                    )
                )

            task = asyncio.create_task(enqueue_responses())
            result = await client.send_command(TOPIC_CMD_FORCE_END)
            await task

        assert result.result_code == CommandResult.CONFLICT

    @pytest.mark.asyncio
    async def test_timeout_sized_barrier_rejects_later_topicless_ack(self) -> None:
        """The ambiguity barrier follows the command timeout, not a short fixed delay."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        with patch("narwal_client.client._TOPICLESS_ACK_QUARANTINE_SECONDS", 0.01):
            with pytest.raises(NarwalCommandError):
                await client.send_command(TOPIC_CMD_FORCE_END, timeout=0.03)

            async def enqueue_responses() -> None:
                await asyncio.sleep(0.02)
                await client._handle_message(self._field5_frame_with_empty_topic())
                await asyncio.sleep(0.02)
                await client._handle_message(
                    self._field5_frame(
                        client._full_topic(TOPIC_CMD_FORCE_END),
                        b"\x08\x03",
                    )
                )

            task = asyncio.create_task(enqueue_responses())
            result = await client.send_command(TOPIC_CMD_FORCE_END)
            await task

        assert result.result_code == CommandResult.CONFLICT

    @pytest.mark.asyncio
    async def test_listener_barrier_preserves_next_topicless_ack(self) -> None:
        """The barrier must drop the stale ACK, not the next command's ACK."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        with patch("narwal_client.client._TOPICLESS_ACK_QUARANTINE_SECONDS", 0.01):
            with pytest.raises(NarwalCommandError):
                await client.send_command(TOPIC_CMD_FORCE_END, timeout=0.001)

            async def enqueue_responses() -> None:
                await asyncio.sleep(0)
                await client._handle_message(self._field5_frame_with_empty_topic())
                await asyncio.sleep(0.02)
                await client._handle_message(self._field5_frame_with_empty_topic())

            task = asyncio.create_task(enqueue_responses())
            result = await client.send_command(TOPIC_CMD_FORCE_END)
            await task

        assert result.success

    @pytest.mark.asyncio
    async def test_direct_recv_barrier_preserves_next_topicless_ack(self) -> None:
        """Direct receive mode drains the stale ACK before sending again."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        frames: asyncio.Queue[bytes] = asyncio.Queue()

        async def recv() -> bytes:
            return await frames.get()

        client._ws.recv = recv

        with patch("narwal_client.client._TOPICLESS_ACK_QUARANTINE_SECONDS", 0.01):
            with pytest.raises(NarwalCommandError):
                await client.send_command(TOPIC_CMD_FORCE_END, timeout=0.001)

            async def enqueue_responses() -> None:
                await asyncio.sleep(0)
                await frames.put(self._field5_frame_with_empty_topic())
                await frames.put(
                    self._broadcast_frame(
                        client._full_topic("status/download_status"),
                        b"\x18\x01",
                    )
                )
                await asyncio.sleep(0.02)
                await frames.put(self._field5_frame_with_empty_topic())

            client.on_state_update = MagicMock()
            task = asyncio.create_task(enqueue_responses())
            result = await client.send_command(TOPIC_CMD_FORCE_END)
            await task

        assert result.success
        assert client.state.download_status == 1
        client.on_state_update.assert_called_once_with(client.state)

    def test_topicless_ack_does_not_match_picture_query(self) -> None:
        """Image-producing commands must wait for their own field5 response."""
        msg = parse_frame(self._field5_frame_with_empty_topic())

        assert not NarwalClient._field5_response_matches(msg, TOPIC_CMD_TAKE_PICTURE)

    @pytest.mark.asyncio
    async def test_listener_mode_discards_unmatched_empty_response_topic(self) -> None:
        """Empty-topic responses are useful only while a command is pending."""
        client = NarwalClient("10.0.0.1", device_id="device")

        await client._handle_message(self._field5_frame_with_empty_topic())
        await client._handle_message(self._field5_frame(client._full_topic("response")))

        assert client._response_queues == {}

    @pytest.mark.asyncio
    async def test_listener_mode_accepts_response_suffix_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = True

        async def enqueue_expected() -> None:
            await asyncio.sleep(0)
            await client._handle_message(
                self._field5_frame(client._full_topic(f"{TOPIC_CMD_FORCE_END}/response"))
            )

        task = asyncio.create_task(enqueue_expected())
        result = await client.send_command(TOPIC_CMD_FORCE_END)
        await task

        assert result.success

    @pytest.mark.asyncio
    async def test_wake_burst_sends_passive_frames_without_ack_waits(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        send_command = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        with (
            patch.object(client, "send_command", send_command),
            patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await client._send_wake_burst()

        assert client._ws.send.await_count == 4
        sent_topics = [
            parse_frame(call.args[0]).short_topic
            for call in client._ws.send.await_args_list
        ]
        assert sent_topics == [
            TOPIC_CMD_NOTIFY_APP_EVENT,
            TOPIC_CMD_ACTIVE_ROBOT,
            TOPIC_CMD_ACTIVE_ROBOT,
            TOPIC_CMD_APP_HEARTBEAT,
        ]
        send_command.assert_awaited_once_with(
            TOPIC_CMD_GET_BASE_STATUS,
            timeout=2.0,
            wait_if_busy=False,
        )
        assert client._topicless_ack_quarantine_until > 0

    @pytest.mark.asyncio
    async def test_subscribe_to_topics_uses_command_channel(self) -> None:
        """Subscription ACKs must be owned instead of racing the next query."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            assert await client.subscribe_to_topics(duration=123)

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == TOPIC_CMD_ACTIVE_ROBOT
        assert mock_send.await_args.kwargs == {
            "timeout": 2.0,
            "wait_if_busy": False,
        }

    @pytest.mark.asyncio
    async def test_subscribe_to_topics_rejects_failed_ack(self) -> None:
        """A rejected subscription ACK must leave retry timing unchanged."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(result_code=CommandResult.NOT_READY)
            assert not await client.subscribe_to_topics(duration=123)

    @pytest.mark.asyncio
    async def test_wake_burst_reports_failed_full_subscription_send(self) -> None:
        """Only the full active_robot subscription frame counts as a renewal."""
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._ws.send.side_effect = [
            None,
            RuntimeError("subscription failed"),
            None,
            None,
        ]

        with (
            patch.object(
                client,
                "send_command",
                new_callable=AsyncMock,
                return_value=CommandResponse(result_code=CommandResult.SUCCESS),
            ),
            patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert not await client._send_wake_burst()

    @pytest.mark.asyncio
    async def test_wake_burst_sends_frames_when_command_busy(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()

        await client._command_lock.acquire()
        try:
            with patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock):
                assert await client._send_wake_burst()
        finally:
            client._command_lock.release()

        assert client._ws.send.await_count == 4

    @pytest.mark.asyncio
    async def test_direct_recv_ignores_field5_for_other_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        client._ws.recv = AsyncMock(
            side_effect=[
                self._field5_frame(
                    client._full_topic(TOPIC_CMD_GET_BASE_STATUS),
                    b"\x08\x03",
                ),
                self._field5_frame(client._full_topic(TOPIC_CMD_FORCE_END)),
            ]
        )

        result = await client.send_command(TOPIC_CMD_FORCE_END)

        assert result.success
        assert client._ws.recv.await_count == 2
        queued = client._response_queue_for(TOPIC_CMD_GET_BASE_STATUS).get_nowait()
        assert queued.message.short_topic == TOPIC_CMD_GET_BASE_STATUS

    @pytest.mark.asyncio
    async def test_direct_recv_accepts_empty_response_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        client._ws.recv = AsyncMock(return_value=self._field5_frame_with_empty_topic())

        result = await client.send_command(TOPIC_CMD_FORCE_END)

        assert result.success

    @pytest.mark.asyncio
    async def test_direct_recv_accepts_response_suffix_topic(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        client._ws.recv = AsyncMock(
            return_value=self._field5_frame(client._full_topic(f"{TOPIC_CMD_FORCE_END}/response"))
        )

        result = await client.send_command(TOPIC_CMD_FORCE_END)

        assert result.success

    @pytest.mark.asyncio
    async def test_malformed_command_response_raises(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        client._ws.recv = AsyncMock(
            return_value=self._field5_frame(client._full_topic(TOPIC_CMD_FORCE_END))
        )

        with (
            patch.object(client, "_decode_protobuf", side_effect=ValueError("bad")),
            pytest.raises(NarwalCommandError, match="Could not decode response"),
        ):
            await client.send_command(TOPIC_CMD_FORCE_END)

    @pytest.mark.asyncio
    async def test_direct_recv_preserves_expected_topic_after_broadcast(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="device")
        client._ws = AsyncMock()
        client._connected.set()
        client._listener_active = False
        client._ws.recv = AsyncMock(
            side_effect=[
                self._broadcast_frame(
                    client._full_topic("status/download_status"),
                    b"\x18\x01",
                ),
                self._field5_frame(client._full_topic(TOPIC_CMD_FORCE_END)),
            ]
        )

        client.on_state_update = MagicMock()
        result = await client.send_command(TOPIC_CMD_FORCE_END)

        assert result.success
        assert client._ws.recv.await_count == 2
        assert client.state.download_status == 1
        client.on_state_update.assert_called_once_with(client.state)
        assert client.robot_awake


class TestConsumableInfoQuery:
    """Tests for local consumable-info polling."""

    @pytest.mark.asyncio
    async def test_failed_consumable_info_query_preserves_existing_alerts(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.maintain_items = [4]
        client.state.replace_items = [20]

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.NOT_READY,
                data={},
            )

            await client.get_consumable_info()

        mock_send.assert_awaited_once()
        assert client.state.maintain_items == [4]
        assert client.state.replace_items == [20]


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
