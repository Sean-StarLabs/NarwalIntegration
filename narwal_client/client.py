"""WebSocket client for Narwal robot vacuum."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import websockets
import websockets.exceptions

from .const import (
    BROADCAST_STALE_TIMEOUT,
    COMMAND_RESPONSE_TIMEOUT,
    DEFAULT_PORT,
    HEARTBEAT_INTERVAL,
    KEEPALIVE_INTERVAL,
    KNOWN_PRODUCT_KEYS,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_INITIAL_DELAY,
    RECONNECT_MAX_DELAY,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_AMBIENT_LIGHT_CTRL,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_DRY_DUST_BAG,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DRY_STATION_BAG,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_ALL_MAPS,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
    TOPIC_CMD_GET_CONSUMABLE_INFO,
    TOPIC_CMD_GET_CURRENT_TASK,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_GET_ROBOT_TASK_STATUS,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESET_CONSUMABLE_INFO,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_PLAN_START,
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_TAKE_PICTURE,
    TOPIC_CMD_GET_DEBUG_IMAGE,
    TOPIC_CMD_SET_LED,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS,
    TOPIC_CMD_YELL,
    TOPIC_POINT_NAVI_PLAN_TRAJ,
    TOPIC_PLANNING_DEBUG,
    TOPIC_ROBOT_CURRENT_STATUS,
    TOPIC_ROBOT_STATUS,
    TOPIC_ROBOT_TASK_STATUS,
    TOPIC_TIMELINE_STATUS,
    DEFAULT_TOPIC_PREFIX,
    WAKE_TIMEOUT,
    AmbientLightCtrlType,
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
    WorkingStatus,
)
from .models import CommandResponse, DeviceInfo, MapData, MapDisplayData, NarwalState
from .protocol import (
    PROTOBUF_FIELD5_TAG,
    NarwalMessage,
    ProtocolError,
    build_frame,
    parse_frame,
)

_LOGGER = logging.getLogger(__name__)

_STALE_DOCK_BASE_STATUSES = {
    WorkingStatus.UNKNOWN,
    WorkingStatus.STANDBY,
    WorkingStatus.DOCKED,
    WorkingStatus.CHARGED,
    WorkingStatus.DOCKED_V2,
}

_FIELD5_RESPONSE_SUFFIX = "/response"
_TOPICLESS_ACK_QUARANTINE_SECONDS = COMMAND_RESPONSE_TIMEOUT

_TOPICLESS_ACK_TOPICS = {
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_AMBIENT_LIGHT_CTRL,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_DRY_DUST_BAG,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DRY_STATION_BAG,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_PLAN_START,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESET_CONSUMABLE_INFO,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_LED,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS,
    TOPIC_CMD_YELL,
}

_AUX_STATUS_TOPICS = {
    TOPIC_TIMELINE_STATUS,
    TOPIC_POINT_NAVI_PLAN_TRAJ,
    TOPIC_PLANNING_DEBUG,
    TOPIC_ROBOT_STATUS,
    TOPIC_ROBOT_CURRENT_STATUS,
    TOPIC_ROBOT_TASK_STATUS,
}

_DOCK_TASK_REFRESH_DELAY = 6.0
_OPTIONAL_TASK_DETAIL_TIMEOUT = 3.0
_DOCK_TASK_FORCE_END_PAYLOADS = {
    # Live-validated on Flow 2: the app's ForceEndTask.Request uses field 1
    # with ParallelTaskType.DRY_STATION_BAG to stop dock-bag drying.
    "dry_dock_bag": b"\x08\x01",
}
_DISPLAY_MAP_MOVE_DELTA = 0.05


def _display_map_robot_moved(
    previous: MapDisplayData | None,
    current: MapDisplayData,
) -> bool:
    """Return true when display-map robot pose changed enough to prove movement."""
    if previous is None:
        return False
    if (
        (previous.robot_x == 0.0 and previous.robot_y == 0.0)
        or (current.robot_x == 0.0 and current.robot_y == 0.0)
    ):
        return False
    if not all(
        math.isfinite(value)
        for value in (
            previous.robot_x,
            previous.robot_y,
            current.robot_x,
            current.robot_y,
        )
    ):
        return False
    return (
        math.hypot(
            current.robot_x - previous.robot_x,
            current.robot_y - previous.robot_y,
        )
        >= _DISPLAY_MAP_MOVE_DELTA
    )


@dataclass
class RoomCleanSettings:
    """Clean parameters for a single room in a custom clean task."""

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1
    route: CleaningRoute | None = None

@dataclass(frozen=True)
class _QueuedResponse:
    """A command response with the time it reached the listener."""

    received_at: float
    message: NarwalMessage


def _base_status_working_status(decoded: dict[str, Any] | object) -> WorkingStatus | None:
    """Extract robot_base_status field 3.1."""
    if not isinstance(decoded, dict):
        return None
    field3 = decoded.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict) or "1" not in field3:
        return None
    try:
        return WorkingStatus(int(field3["1"]))
    except (TypeError, ValueError):
        return None


def _base_status_confirms_docked(
    decoded: dict[str, Any] | object, status: WorkingStatus | None
) -> bool:
    """Return true when a terminal status also carries live dock indicators."""
    if not isinstance(decoded, dict) or status not in _STALE_DOCK_BASE_STATUSES:
        return False

    field3 = decoded.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    field3 = field3 if isinstance(field3, dict) else {}

    def int_field(container: dict[str, Any], field: str) -> int:
        try:
            return int(container.get(field, 0))
        except (TypeError, ValueError):
            return 0

    return (
        int_field(decoded, "11") >= 2
        or int_field(decoded, "47") in (1, 3)
        or int_field(field3, "3") in (1, 6)
        or int_field(field3, "10") == 1
        or int_field(field3, "12") > 0
    )


class NarwalConnectionError(Exception):
    """Raised when connection to the vacuum fails."""


class NarwalCommandError(Exception):
    """Raised when a command fails or times out."""


class NarwalClient:
    """Async WebSocket client for communicating with a Narwal vacuum.

    Usage:
        client = NarwalClient(host="192.168.1.100", device_id="your_device_id")
        await client.connect()
        client.on_state_update = my_callback
        await client.start_listening()
        # ...later...
        await client.disconnect()
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        device_id: str = "",
        topic_prefix: str | None = None,
        supports_broadcasts: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.url = f"ws://{host}:{port}"
        self.topic_prefix = topic_prefix or DEFAULT_TOPIC_PREFIX
        self.supports_broadcasts = supports_broadcasts
        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None
        self.on_message: Callable[[NarwalMessage], None] | None = None

        self._ws: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._should_reconnect = True
        self._listener_active = False  # True when start_listening() is running recv loop
        self._robot_awake = False  # True after a broadcast or addressed response
        self._last_broadcast_time: float = 0.0  # monotonic time of last broadcast
        self._last_response_time: float = 0.0  # monotonic time of last addressed response
        self._last_display_map_time: float = 0.0  # monotonic time of last display_map
        # Queues for field5 command responses, keyed by command topic.
        self._response_queues: dict[str, asyncio.Queue[_QueuedResponse]] = {}
        self._pending_response_topic: str | None = None
        self._topicless_ack_quarantine_until: float = 0.0
        # Lock to prevent concurrent send_command calls from racing on the queue
        self._command_lock = asyncio.Lock()

    def _full_topic(self, short_topic: str) -> str:
        """Build the full topic path."""
        return f"{self.topic_prefix}/{self.device_id}/{short_topic}"

    def _update_topic_prefix_from_topic(self, topic: str, source: str) -> None:
        """Preserve the product-key prefix from a robot response topic."""
        parts = topic.split("/")
        if len(parts) >= 2 and parts[0] == "" and parts[1]:
            self.topic_prefix = f"/{parts[1]}"
            _LOGGER.info("Topic prefix from %s: %s", source, self.topic_prefix)

    def _response_queue_for(self, short_topic: str) -> asyncio.Queue[_QueuedResponse]:
        """Return the field5 response queue for one command topic."""
        if short_topic not in self._response_queues:
            self._response_queues[short_topic] = asyncio.Queue()
        return self._response_queues[short_topic]

    @staticmethod
    def _field5_response_matches(msg: NarwalMessage, short_topic: str) -> bool:
        """Return True when a field5 frame can satisfy a command wait.

        Most robot responses echo the command topic, but captured ACK frames may
        also use an empty topic or append /response. Empty-topic frames are only
        accepted for known ACK-style commands so delayed fire-and-forget replies
        cannot satisfy data queries.
        """
        if msg.field_tag != PROTOBUF_FIELD5_TAG:
            return False
        if NarwalClient._is_topicless_field5_response(msg):
            return short_topic in _TOPICLESS_ACK_TOPICS
        response_topic = msg.short_topic.strip("/")
        if response_topic == short_topic:
            return True
        return response_topic == f"{short_topic}{_FIELD5_RESPONSE_SUFFIX}"

    @staticmethod
    def _is_topicless_field5_response(msg: NarwalMessage) -> bool:
        """Return True for ACK frames that do not identify their command topic."""
        if msg.field_tag != PROTOBUF_FIELD5_TAG:
            return False
        response_topic = msg.short_topic.strip("/")
        return not response_topic or response_topic == "response"

    @staticmethod
    def _looks_like_map_response(decoded: Mapping[str, Any]) -> bool:
        """Return True for a topicless field5 payload shaped like get_map data."""
        payload = decoded.get("2")
        if not isinstance(payload, Mapping):
            return False
        return "1" in payload and any(key in payload for key in ("4", "5", "12", "17"))

    def _topicless_data_response_matches(
        self,
        msg: NarwalMessage,
        short_topic: str,
    ) -> bool:
        """Return True when a topicless data frame safely matches a pending query."""
        if (
            not self._is_topicless_field5_response(msg)
            or self._topicless_ack_is_quarantined(msg)
        ):
            return False
        if short_topic != TOPIC_CMD_GET_MAP:
            return False
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            return False
        return self._looks_like_map_response(decoded)

    def _topicless_ack_is_quarantined(self, msg: NarwalMessage) -> bool:
        """Return True when a late optional ACK should be discarded."""
        return (
            self._is_topicless_field5_response(msg)
            and time.monotonic() < self._topicless_ack_quarantine_until
        )

    def _quarantine_topicless_acks(self, timeout: float) -> None:
        """Reject topicless ACKs after an ambiguous command timeout."""
        self._topicless_ack_quarantine_until = max(
            self._topicless_ack_quarantine_until,
            time.monotonic() + timeout,
        )

    async def _wait_for_topicless_ack_barrier(self, short_topic: str) -> None:
        """Drain ambiguous topicless ACKs before sending another ACK command."""
        if short_topic not in _TOPICLESS_ACK_TOPICS:
            return
        deadline = self._topicless_ack_quarantine_until
        now = time.monotonic()
        if deadline <= now:
            self._topicless_ack_quarantine_until = 0.0
            return

        if self._listener_active:
            await asyncio.sleep(deadline - now)
            self._topicless_ack_quarantine_until = 0.0
            return

        drained = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(), timeout=min(remaining, 0.05)
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            if not isinstance(data, bytes) or len(data) < 4:
                continue
            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue

            if self._is_topicless_field5_response(msg):
                drained += 1
                continue

            await self._handle_message(data)

        self._topicless_ack_quarantine_until = 0.0
        if drained:
            _LOGGER.debug("Drained %d ambiguous topicless ACKs", drained)

    def _field5_response_queue_topic(self, msg: NarwalMessage) -> str | None:
        """Return the queue topic for a field5 response frame."""
        if self._topicless_ack_is_quarantined(msg):
            _LOGGER.debug(
                "Discarding quarantined topicless field5 ACK during command wait"
            )
            return None
        pending = self._pending_response_topic
        if pending is not None:
            if self._field5_response_matches(
                msg, pending
            ) or self._topicless_data_response_matches(msg, pending):
                return pending
        response_topic = msg.short_topic.strip("/")
        if not response_topic or response_topic == "response":
            return None
        if response_topic.endswith(_FIELD5_RESPONSE_SUFFIX):
            return response_topic[: -len(_FIELD5_RESPONSE_SUFFIX)]
        return response_topic

    @property
    def connected(self) -> bool:
        """Return True if the WebSocket is currently connected."""
        return self._ws is not None and self._connected.is_set()

    @property
    def robot_awake(self) -> bool:
        """Return True if the robot has confirmed local reachability."""
        return self._robot_awake

    def _mark_response_received(self) -> None:
        """Record an addressed response as reachability for non-broadcast models."""
        self._last_response_time = time.monotonic()
        if not self.supports_broadcasts:
            self._robot_awake = True

    @property
    def last_broadcast_age(self) -> float:
        """Seconds since last broadcast (0.0 if none received yet)."""
        if self._last_broadcast_time <= 0:
            return 0.0
        return time.monotonic() - self._last_broadcast_time

    @property
    def last_display_map_age(self) -> float:
        """Seconds since last display_map broadcast (999.0 if none received)."""
        if self._last_display_map_time <= 0:
            return 999.0
        return time.monotonic() - self._last_display_map_time

    def _update_from_working_status_broadcast(self, decoded: dict[str, Any]) -> None:
        """Apply live task metrics from the working_status broadcast."""
        self.state.update_from_working_status(decoded)

    def _update_from_base_status_broadcast(self, decoded: dict[str, Any]) -> None:
        """Apply a base-status broadcast without clobbering fresh task metrics."""
        status = _base_status_working_status(decoded)
        if (
            self.state.has_recent_active_working_status
            and status in _STALE_DOCK_BASE_STATUSES
            and not _base_status_confirms_docked(decoded, status)
        ):
            self.state.update_battery_from_base_status(decoded)
            _LOGGER.debug(
                "Ignoring stale base_status=%s while working_status task metrics are fresh",
                status.name if status else "unknown",
            )
            return

        self.state.update_from_base_status(decoded)

    async def connect(self) -> None:
        """Establish WebSocket connection to the vacuum.

        Raises:
            NarwalConnectionError: If connection cannot be established.
        """
        try:
            self._ws = await websockets.connect(
                self.url, ping_interval=30, ping_timeout=10
            )
            self._connected.set()
            _LOGGER.info("Connected to Narwal vacuum at %s", self.url)
        except (OSError, websockets.exceptions.WebSocketException) as e:
            raise NarwalConnectionError(
                f"Failed to connect to {self.url}: {e}"
            ) from e

    async def discover_device_id(self, timeout: float = 15.0) -> str:
        """Discover the device_id by waking the robot and reading its response.

        The robot sleeps when idle and won't broadcast until woken. This method
        sends a get_device_info command (with empty device_id) as a wake signal.
        The robot's local WebSocket server processes commands regardless of the
        device_id in the topic. The response contains the real device_id.

        Falls back to extracting device_id from broadcast topics if the
        command response doesn't contain it.

        Args:
            timeout: Seconds to wait for discovery.

        Returns:
            The device_id string.

        Raises:
            NarwalConnectionError: If not connected.
            NarwalCommandError: If discovery fails within timeout.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        # Build wake frames using all known product key prefixes.
        # The robot only responds to commands with its correct product key
        # in the topic. Since we don't know the model yet, try all known
        # keys until one provokes a response.
        cmd = TOPIC_CMD_GET_DEVICE_INFO
        wake_frames = [
            build_frame(self._full_topic(cmd), b""),  # current prefix (default or user-set)
            build_frame(f"//{cmd}", b""),  # bare topic, no prefix
        ]
        # Add frames for all known product keys (skip default, already included)
        for key in KNOWN_PRODUCT_KEYS:
            if key != self.topic_prefix.lstrip("/"):
                wake_frames.append(
                    build_frame(f"/{key}/{self.device_id}/{cmd}", b"")
                )
        # Send first batch (default + bare + first few known keys)
        batch_size = min(5, len(wake_frames))
        for frame in wake_frames[:batch_size]:
            try:
                await self._ws.send(frame)
            except Exception as e:
                _LOGGER.warning("Failed to send wake command: %s", e)
        _LOGGER.debug(
            "Sent discovery wake commands (%d prefixes, device_id='%s')",
            batch_size, self.device_id,
        )

        wake_index = 0  # cycle through wake frames on retry
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(), timeout=min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                # Re-send wake commands, cycling through prefixes
                try:
                    await self._ws.send(wake_frames[wake_index % len(wake_frames)])
                    wake_index += 1
                    _LOGGER.debug("Re-sent wake-up command (variant %d)", wake_index)
                except Exception:
                    pass
                continue

            if not isinstance(data, bytes) or len(data) < 4:
                continue

            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue

            # Check field5 response — get_device_info returns device_id in field 2
            if msg.field_tag == PROTOBUF_FIELD5_TAG and msg.payload:
                try:
                    decoded = self._decode_protobuf(msg.payload)
                    raw_id = decoded.get("2", b"")
                    if isinstance(raw_id, bytes):
                        raw_id = raw_id.decode("utf-8", errors="replace").strip()
                    else:
                        raw_id = str(raw_id).strip()
                    if raw_id:
                        self._update_topic_prefix_from_topic(msg.topic, "response")
                        self.device_id = raw_id
                        _LOGGER.info("Discovered device_id from response: %s", self.device_id)
                        return self.device_id
                except Exception:
                    _LOGGER.debug("Failed to decode response payload")

            # Fallback: broadcast messages (field4/0x22) have device_id in topic
            if msg.field_tag != PROTOBUF_FIELD5_TAG and msg.topic:
                parts = msg.topic.split("/")
                # Topic format: /{product_key}/{device_id}/{category}/{type}
                if len(parts) >= 4 and parts[2]:
                    # Extract product_key from topic to set correct prefix
                    self._update_topic_prefix_from_topic(msg.topic, "broadcast")
                    self.device_id = parts[2]
                    _LOGGER.info("Discovered device_id from broadcast: %s", self.device_id)
                    return self.device_id

        raise NarwalCommandError(
            f"No response or broadcast within {timeout}s — check vacuum IP and power"
        )

    async def drain_ws_buffer(self) -> None:
        """Drain any pending messages from the WebSocket receive buffer.

        Called between discover_device_id() and send_command() to clear
        stale field5 responses left by wake probe commands. Without this,
        _wait_for_field5_response may consume a stale response instead of
        the real one, which can have unexpected data or error codes.
        """
        if not self.connected:
            return
        drained = 0
        while True:
            try:
                await asyncio.wait_for(self._ws.recv(), timeout=0.05)
                drained += 1
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        if drained:
            _LOGGER.debug("Drained %d stale messages from WebSocket buffer", drained)

    async def disconnect(self) -> None:
        """Disconnect from the vacuum and stop all tasks."""
        self._should_reconnect = False
        self._listener_active = False
        self._robot_awake = False
        self._connected.clear()

        for task in (self._heartbeat_task, self._keepalive_task, self._listen_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        _LOGGER.info("Disconnected from Narwal vacuum")

    async def start_listening(self) -> None:
        """Start the persistent message listener with auto-reconnect.

        This method runs indefinitely until disconnect() is called.
        """
        self._should_reconnect = True
        retry_delay = RECONNECT_INITIAL_DELAY

        while self._should_reconnect:
            try:
                if not self.connected:
                    await self.connect()
                    # Immediate wake burst on (re)connect — the fresh TCP
                    # connection may trigger the robot's deep-sleep wake
                    # interrupt, but only if we send commands before it
                    # expires.  Don't wait for the keepalive loop's first
                    # tick (15s delay would be too late).
                    if self.supports_broadcasts:
                        await self._send_wake_burst()

                retry_delay = RECONNECT_INITIAL_DELAY  # reset on success
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                self._listener_active = True

                async for raw_message in self._ws:
                    if isinstance(raw_message, bytes):
                        await self._handle_message(raw_message)

            except NarwalConnectionError as e:
                _LOGGER.warning("Connection failed: %s", e)
            except websockets.exceptions.ConnectionClosed as e:
                _LOGGER.warning("Connection closed: %s", e)
            except asyncio.CancelledError:
                _LOGGER.debug("Listener cancelled")
                return
            except Exception:
                _LOGGER.exception("Unexpected error in listener")
            finally:
                self._listener_active = False
                self._robot_awake = False
                self._connected.clear()
                for task in (self._heartbeat_task, self._keepalive_task):
                    if task and not task.done():
                        task.cancel()

            if not self._should_reconnect:
                break

            # Exponential backoff with jitter
            jitter = random.uniform(0, 1)
            wait = retry_delay + jitter
            _LOGGER.info("Reconnecting in %.1fs...", wait)
            await asyncio.sleep(wait)
            retry_delay = min(
                retry_delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY
            )

    async def _handle_message(self, data: bytes) -> None:
        """Parse a raw frame and update state or route response."""
        if len(data) < 4:
            return

        try:
            msg = parse_frame(data)
        except ProtocolError as e:
            _LOGGER.debug("Failed to parse frame: %s", e)
            return

        # Field5 (0x2a) messages are command responses
        if msg.field_tag == PROTOBUF_FIELD5_TAG:
            queue_topic = self._field5_response_queue_topic(msg)
            if queue_topic is None:
                _LOGGER.debug(
                    "Discarding unmatched field5 response with frame topic=%s",
                    msg.short_topic,
                )
                return
            queue = self._response_queue_for(queue_topic)
            _LOGGER.debug(
                "Field5 response routed to queue: %s (frame topic=%s)",
                queue_topic,
                msg.short_topic,
            )
            await queue.put(_QueuedResponse(time.monotonic(), msg))
            self._mark_response_received()
            return

        # Any broadcast means the robot is awake
        self._last_broadcast_time = time.monotonic()
        if not self._robot_awake:
            self._robot_awake = True
            _LOGGER.info("Robot is awake (received broadcast)")

        if self.on_message:
            self.on_message(msg)

        # Decode protobuf and update state based on topic
        short_topic = msg.short_topic
        _LOGGER.debug("Broadcast topic: %s (tag=0x%02x)", short_topic, msg.field_tag)
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return

        if short_topic == "status/working_status":
            self._update_from_working_status_broadcast(decoded)
        elif short_topic == "status/robot_base_status":
            self._update_from_base_status_broadcast(decoded)
        elif short_topic == "upgrade/upgrade_status":
            self.state.update_from_upgrade_status(decoded)
        elif short_topic == "status/download_status":
            self.state.update_from_download_status(decoded)
        elif short_topic == "map/display_map":
            self._last_display_map_time = time.monotonic()
            display = MapDisplayData.from_broadcast(decoded)
            if _display_map_robot_moved(self.state.map_display_data, display):
                self.state.last_map_robot_movement = self._last_display_map_time
            self.state.map_display_data = display
            if self.state.map_display_data.trajectory:
                self.state.native_trajectory = self.state.map_display_data.trajectory
                self.state.native_trajectory_updated = self._last_display_map_time
            _LOGGER.debug(
                "display_map received: robot=(%.2f, %.2f) ts=%d",
                self.state.map_display_data.robot_x,
                self.state.map_display_data.robot_y,
                self.state.map_display_data.timestamp,
            )
        elif short_topic in _AUX_STATUS_TOPICS:
            self.state.update_from_aux_status(short_topic, decoded)
        if self.on_state_update:
            self.on_state_update(self.state)

    def _decode_protobuf(self, payload: bytes) -> dict[str, Any]:
        """Decode a protobuf payload without a schema using blackboxprotobuf."""
        import blackboxprotobuf  # lazy import — heavy dependency

        decoded, _ = blackboxprotobuf.decode_message(payload)
        return decoded

    async def _heartbeat_loop(self) -> None:
        """Send periodic WebSocket pings to keep the connection alive."""
        try:
            while self.connected:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws:
                    await self._ws.ping()
                    _LOGGER.debug("Heartbeat ping sent")
        except asyncio.CancelledError:
            return
        except Exception:
            _LOGGER.debug("Heartbeat failed, connection may be lost")

    # --- Wake / Keep-alive ---

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint."""
        result = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @classmethod
    def _encode_varint_field(cls, field_num: int, value: int) -> bytes:
        """Encode a protobuf varint field (tag + value)."""
        tag = (field_num << 3) | 0  # wire type 0 = varint
        return cls._encode_varint(tag) + cls._encode_varint(value)

    @classmethod
    def _encode_bytes_field(cls, field_num: int, data: bytes) -> bytes:
        """Encode a protobuf length-delimited field."""
        tag = (field_num << 3) | 2  # wire type 2 = length-delimited
        return cls._encode_varint(tag) + cls._encode_varint(len(data)) + data

    @classmethod
    def _encode_string_field(cls, field_num: int, text: str) -> bytes:
        """Encode a protobuf string field."""
        return cls._encode_bytes_field(field_num, text.encode("utf-8"))

    @classmethod
    def _encode_packed_varint_field(cls, field_num: int, values: Iterable[int]) -> bytes:
        """Encode a packed repeated varint field."""
        data = b"".join(cls._encode_varint(value) for value in values)
        return cls._encode_bytes_field(field_num, data)

    @classmethod
    def _build_reset_consumable_info_payload(
        cls,
        *,
        maintain_items: Iterable[int] = (),
        replace_items: Iterable[int] = (),
    ) -> bytes:
        """Build a ConsumableInfoPayload for reset_consumable_info.

        get_consumable_info returns field 1 as a ConsumableInfoPayload whose
        fields 1/2 are packed maintain/replace enum lists. The app's reset
        topic appears to use the same payload shape when clearing individual
        alerts; an empty payload is kept as a fallback for firmwares that only
        support resetting the whole consumable-info alert list.
        """
        maintain = tuple(int(item) for item in maintain_items)
        replace = tuple(int(item) for item in replace_items)
        inner = b""
        if maintain:
            inner += cls._encode_packed_varint_field(1, maintain)
        if replace:
            inner += cls._encode_packed_varint_field(2, replace)
        return cls._encode_bytes_field(1, inner) if inner else b""

    # All broadcast topics the robot can send — used for active_robot_publish
    _ALL_BROADCAST_TOPICS = [
        "status/robot_base_status",
        "status/working_status",
        "upgrade/upgrade_status",
        "status/download_status",
        "map/display_map",
        TOPIC_TIMELINE_STATUS,
        TOPIC_POINT_NAVI_PLAN_TRAJ,
        TOPIC_PLANNING_DEBUG,
        TOPIC_ROBOT_STATUS,
        TOPIC_ROBOT_CURRENT_STATUS,
        TOPIC_ROBOT_TASK_STATUS,
    ]

    def _build_topic_subscription(self, duration: int = 600) -> bytes:
        """Build active_robot_publish payload subscribing to ALL broadcast topics.

        The Narwal app sends this on open to tell the robot which topics to
        broadcast and for how long. Format: repeated field 1 = TopicDuration
        sub-messages with {1: topic_string, 2: duration_seconds}.
        """
        payload = b""
        for topic in self._ALL_BROADCAST_TOPICS:
            inner = (
                self._encode_string_field(1, topic)
                + self._encode_varint_field(2, duration)
            )
            payload += self._encode_bytes_field(1, inner)
        return payload

    async def _send_optional_ack_command(
        self,
        short_topic: str,
        payload: bytes = b"",
        *,
        timeout: float = 1.0,
        label: str,
    ) -> bool:
        """Send an optional ACK-style command while owning the command queue."""
        if not self.connected:
            return False
        try:
            response = await self.send_command(
                short_topic,
                payload,
                timeout=timeout,
                wait_if_busy=False,
            )
        except NarwalCommandError as err:
            if "Command channel busy" not in str(err):
                self._quarantine_topicless_acks(_TOPICLESS_ACK_QUARANTINE_SECONDS)
            _LOGGER.debug("%s skipped/failed for %s: %s", label, short_topic, err)
            return False
        except Exception:
            _LOGGER.debug("%s failed for %s", label, short_topic, exc_info=True)
            return False
        if not response.success:
            _LOGGER.debug(
                "%s rejected for %s: result_code=%s",
                label,
                short_topic,
                response.result_code,
            )
            return False
        return True

    async def _send_wake_frame(self, short_topic: str, payload: bytes) -> bool:
        """Send one fire-and-forget wake candidate frame."""
        if not self.connected or not self._ws:
            return False
        full_topic = self._full_topic(short_topic)
        frame = build_frame(full_topic, payload)
        try:
            await self._ws.send(frame)
        except Exception:
            _LOGGER.debug("Wake burst failed for %s", short_topic, exc_info=True)
            return False
        _LOGGER.debug("Wake burst sent %s (%d bytes)", short_topic, len(frame))
        return True

    async def subscribe_to_topics(self, duration: int = 600) -> bool:
        """Send topic subscription to the robot.

        This tells the robot to broadcast display_map, working_status, etc.
        Must be called after connecting, especially if the robot is already
        awake (wake() skips the burst when robot_awake is True).
        """
        payload = self._build_topic_subscription(duration)
        if not await self._send_optional_ack_command(
            TOPIC_CMD_ACTIVE_ROBOT,
            payload,
            timeout=2.0,
            label="Topic subscription",
        ):
            return False
        _LOGGER.info("Topic subscription refreshed (duration=%ds)", duration)
        return True

    def _build_wake_commands(self) -> list[tuple[str, bytes]]:
        """Build the sequence of wake commands to try.

        Returns list of wake candidate (short_topic, payload) tuples.  The
        get_device_base_status query is sent separately through send_command()
        so any field5 response owns the command lock instead of racing user
        commands with an empty response topic.
        """
        cmds: list[tuple[str, bytes]] = []

        # 1. notify_app_event — signal "app opened" (triggers robot wake)
        cmds.append((TOPIC_CMD_NOTIFY_APP_EVENT, self._encode_varint_field(1, 1)))

        # 2. active_robot_publish — subscribe to ALL topics for 10 minutes
        cmds.append((TOPIC_CMD_ACTIVE_ROBOT, self._build_topic_subscription(600)))

        # 3. active_robot_publish — simple duration (field 1 = 600)
        cmds.append((TOPIC_CMD_ACTIVE_ROBOT, self._encode_varint_field(1, 600)))

        # 4. app heartbeat — field 1 = 1
        cmds.append((TOPIC_CMD_APP_HEARTBEAT, self._encode_varint_field(1, 1)))

        return cmds

    async def _send_wake_burst(self) -> bool:
        """Send all wake candidate commands in quick succession.

        Wake commands are passive probes: sleeping robots often do not ACK them,
        so waiting on each candidate can consume the whole wake timeout. Send the
        burst directly, then quarantine any late topicless ACK before running the
        addressed base-status probe.
        """
        if not self.connected or not self._ws:
            return False

        commands = self._build_wake_commands()
        subscription_sent = False
        for index, (short_topic, payload) in enumerate(commands):
            sent = await self._send_wake_frame(short_topic, payload)
            if index == 1 and sent:
                subscription_sent = True
            await asyncio.sleep(0.2)
        self._quarantine_topicless_acks(_TOPICLESS_ACK_QUARANTINE_SECONDS)

        try:
            await self.send_command(
                TOPIC_CMD_GET_BASE_STATUS,
                timeout=2.0,
                wait_if_busy=False,
            )
            _LOGGER.debug("Wake burst: base status probe completed")
        except NarwalCommandError as err:
            _LOGGER.debug("Wake burst: base status probe skipped/failed: %s", err)
        except Exception:
            _LOGGER.debug("Wake burst: base status probe failed", exc_info=True)
        return subscription_sent

    async def wake(self, timeout: float = WAKE_TIMEOUT, force: bool = False) -> bool:
        """Attempt to wake the robot from sleep.

        Sends repeated bursts of wake commands and waits for the robot to
        start broadcasting status messages.  Does NOT reconnect the
        WebSocket — the keepalive loop handles reconnect escalation
        independently (avoids race conditions with the listener loop).

        Args:
            timeout: Maximum seconds to wait for the robot to respond.
            force: If True, send wake burst even if robot_awake is True.
                Use when broadcasts have gone stale but the flag hasn't
                been reset yet.

        Returns:
            True if the robot has confirmed local reachability, False otherwise.
        """
        if not self.supports_broadcasts:
            return self.connected
        if self._robot_awake and not force:
            return True

        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        _LOGGER.info("Attempting to wake robot (timeout=%.0fs)...", timeout)

        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0

        while asyncio.get_event_loop().time() < deadline:
            attempt += 1

            if not self.connected:
                _LOGGER.debug("Connection lost during wake — aborting")
                break

            await self._send_wake_burst()

            # Wait up to 5 seconds for a broadcast to arrive
            wait_end = min(
                asyncio.get_event_loop().time() + 5.0,
                deadline,
            )
            while asyncio.get_event_loop().time() < wait_end:
                if self._robot_awake:
                    _LOGGER.info("Robot woke up after %d attempt(s)", attempt)
                    return True
                await asyncio.sleep(0.3)

        _LOGGER.warning("Robot did not wake up within %.0fs (%d attempts)", timeout, attempt)
        return False

    # Topic subscription duration (seconds) and renewal interval
    _TOPIC_SUB_DURATION = 600  # 10 minutes — matches what Narwal app sends
    _TOPIC_RESUB_INTERVAL = 480  # re-subscribe every 8 min (before 10min expiry)

    # After this many consecutive wake bursts without response (~60s),
    # force a WebSocket reconnect to try triggering the robot's deep sleep
    # wake handler via a fresh TCP connection.
    _WAKE_RECONNECT_THRESHOLD = 2

    async def _keepalive_loop(self) -> None:
        """Periodically send wake/heartbeat commands to prevent robot from sleeping.

        Runs alongside the listener loop. Sends a lightweight heartbeat
        command every KEEPALIVE_INTERVAL seconds. If the robot stops
        broadcasting for BROADCAST_STALE_TIMEOUT seconds (goes back to
        sleep), resets _robot_awake and escalates to a full wake burst.

        Also re-subscribes to broadcast topics before the subscription
        expires (every _TOPIC_RESUB_INTERVAL seconds) so that display_map,
        robot_base_status, etc. keep flowing during long cleaning sessions.

        If wake bursts fail repeatedly, forces a WebSocket reconnect by
        closing the connection (the listener loop handles reconnection).
        """
        # Start at 0 so the first keepalive tick sends the subscription
        # immediately. This handles the case where the robot is already
        # broadcasting (e.g. mid-cleaning) and wake() skips the burst.
        last_resub_time = 0.0
        consecutive_wake_failures = 0
        try:
            while self.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if not self.connected or not self._ws:
                    break

                if not self.supports_broadcasts:
                    continue

                # Check if broadcasts have gone stale (robot fell back asleep)
                if (
                    self._robot_awake
                    and self._last_broadcast_time > 0
                    and time.monotonic() - self._last_broadcast_time
                    > BROADCAST_STALE_TIMEOUT
                ):
                    _LOGGER.info(
                        "No broadcast for %.0fs — robot may have gone to sleep",
                        time.monotonic() - self._last_broadcast_time,
                    )
                    self._robot_awake = False
                    consecutive_wake_failures = 0

                if self._robot_awake:
                    consecutive_wake_failures = 0
                    # Re-subscribe to topics before the subscription expires
                    if time.monotonic() - last_resub_time > self._TOPIC_RESUB_INTERVAL:
                        if await self.subscribe_to_topics(self._TOPIC_SUB_DURATION):
                            last_resub_time = time.monotonic()
                            _LOGGER.debug("Topic subscription renewed")

                    # Send lightweight heartbeat to keep robot awake.
                    # The Narwal app sends this continuously regardless of
                    # robot state — it's safe during cleaning.
                    if await self._send_optional_ack_command(
                        TOPIC_CMD_APP_HEARTBEAT,
                        self._encode_varint_field(1, 1),
                        timeout=1.0,
                        label="Keepalive heartbeat",
                    ):
                        _LOGGER.debug("Keepalive heartbeat sent")
                else:
                    # Robot appears asleep — send full wake burst
                    # (wake burst includes topic subscription)
                    consecutive_wake_failures += 1
                    _LOGGER.debug(
                        "Robot not awake, sending wake burst "
                        "(attempt %d/%d before reconnect)",
                        consecutive_wake_failures,
                        self._WAKE_RECONNECT_THRESHOLD,
                    )
                    if await self._send_wake_burst():
                        last_resub_time = time.monotonic()

                    # Escalation: after repeated failures, force a fresh
                    # WebSocket connection. Close the socket — the listener
                    # loop's reconnect logic will establish a new connection.
                    if consecutive_wake_failures >= self._WAKE_RECONNECT_THRESHOLD:
                        _LOGGER.warning(
                            "Wake burst failed %d times — forcing WebSocket "
                            "reconnect to trigger deep sleep wake",
                            consecutive_wake_failures,
                        )
                        consecutive_wake_failures = 0
                        if self._ws:
                            await self._ws.close()
                        break  # exit keepalive; listener reconnects

        except asyncio.CancelledError:
            return
        except Exception:
            _LOGGER.debug("Keepalive loop error, will restart with listener")

    # --- Command infrastructure ---

    async def send_command(
        self,
        short_topic: str,
        payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
        *,
        wait_if_busy: bool = True,
    ) -> CommandResponse:
        """Send a command and wait for the field5 response.

        Uses a lock to prevent concurrent commands from racing on the
        response queue. Works both with and without start_listening().

        Args:
            short_topic: Command topic without prefix/device_id.
            payload: Protobuf-encoded payload (empty for most commands).
            timeout: Seconds to wait for response.
            wait_if_busy: If false, raise immediately when another command is in
                flight. This is for optional background commands only; user
                actions should keep the default and wait their turn.

        Returns:
            CommandResponse with result code and decoded data.

        Raises:
            NarwalConnectionError: If not connected.
            NarwalCommandError: If response times out.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")
        if not wait_if_busy and self._command_lock.locked():
            raise NarwalCommandError(
                f"Command channel busy; skipping optional command '{short_topic}'"
            )

        async with self._command_lock:
            await self._wait_for_topicless_ack_barrier(short_topic)
            response_queue = self._response_queue_for(short_topic)
            self._pending_response_topic = short_topic
            try:
                # Drain stale responses for this command topic only. Other pending
                # wake/status responses must not satisfy this command.
                drained = 0
                while not response_queue.empty():
                    try:
                        response_queue.get_nowait()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained:
                    _LOGGER.debug(
                        "Drained %d stale field5 responses for %s",
                        drained,
                        short_topic,
                    )

                full_topic = self._full_topic(short_topic)
                frame = build_frame(full_topic, payload)
                sent_at = time.monotonic()
                await self._ws.send(frame)
                _LOGGER.debug("Sent command: %s (%d bytes)", short_topic, len(frame))

                # If listener is running, wait on the queue (avoid concurrent recv)
                try:
                    if self._listener_active:
                        msg = await self._wait_for_queued_response(
                            short_topic,
                            response_queue,
                            sent_at,
                            timeout,
                        )
                    else:
                        # No listener — read directly from websocket
                        msg = await self._wait_for_field5_response(
                            short_topic,
                            timeout,
                        )
                except NarwalCommandError:
                    if short_topic in _TOPICLESS_ACK_TOPICS:
                        self._quarantine_topicless_acks(
                            max(_TOPICLESS_ACK_QUARANTINE_SECONDS, timeout)
                        )
                    raise
            finally:
                self._pending_response_topic = None

            self._mark_response_received()

        # Decode response
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception as err:
            raise NarwalCommandError(
                f"Could not decode response for command '{short_topic}'"
            ) from err

        # Field 1 is a result code for action commands (int),
        # but data for some query commands (string/bytes/dict).
        # Room-clean returns field 1 as a dict (config echo), not an int.
        raw_field1 = decoded.get("1", 0)
        try:
            result_code = int(raw_field1)
        except (ValueError, TypeError):
            result_code = CommandResult.SUCCESS  # non-int field 1 = data response = success

        return CommandResponse(
            result_code=result_code,
            data=decoded,
            raw_payload=msg.payload,
        )

    async def _wait_for_queued_response(
        self,
        short_topic: str,
        response_queue: asyncio.Queue[_QueuedResponse],
        sent_at: float,
        timeout: float,
    ) -> NarwalMessage:
        """Wait for the matching field5 response routed by the listener."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                queued = await asyncio.wait_for(
                    response_queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                break
            if queued.received_at < sent_at:
                _LOGGER.debug("Ignoring pre-send response for %s", short_topic)
                continue
            return queued.message
        raise NarwalCommandError(
            f"No response for command '{short_topic}' within {timeout}s"
        )

    async def _wait_for_field5_response(
        self, short_topic: str, timeout: float
    ) -> NarwalMessage:
        """Read from WebSocket until the expected field5 response arrives."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                continue

            if not isinstance(data, bytes) or len(data) < 4:
                continue

            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue

            if msg.field_tag == PROTOBUF_FIELD5_TAG:
                if self._topicless_ack_is_quarantined(msg):
                    _LOGGER.debug(
                        "Ignoring quarantined topicless field5 response while waiting for %s",
                        short_topic,
                    )
                    continue
                if self._field5_response_matches(
                    msg, short_topic
                ) or self._topicless_data_response_matches(msg, short_topic):
                    return msg
                _LOGGER.debug(
                    "Ignoring field5 response for %s while waiting for %s",
                    msg.short_topic,
                    short_topic,
                )
                queue_topic = self._field5_response_queue_topic(msg)
                if queue_topic is not None:
                    queue = self._response_queue_for(queue_topic)
                    await queue.put(_QueuedResponse(time.monotonic(), msg))
                continue

            await self._handle_message(data)

        raise NarwalCommandError(
            f"No field5 response within {timeout}s"
        )

    async def send_raw(
        self, topic: str, payload: bytes, header_byte: int | None = None
    ) -> None:
        """Send a raw command frame to the vacuum.

        Args:
            topic: Full topic string.
            payload: Protobuf-encoded payload.
            header_byte: Header byte (auto-calculated if None).

        Raises:
            NarwalConnectionError: If not connected.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        frame = build_frame(topic, payload, header_byte)
        await self._ws.send(frame)
        _LOGGER.debug("Sent raw to topic: %s (%d bytes)", topic, len(frame))

    # --- High-level commands ---

    async def locate(self) -> CommandResponse:
        """Trigger locate sound — robot says 'Robot is here'."""
        return await self.send_command(TOPIC_CMD_YELL)

    # Legacy clean task payload — the minimal CleanTask the app sent on Flow
    # firmware < v01.07.22. Only used by the saved-plan fallback below, which
    # goes to clean/plan/start — a topic that discards payloads entirely (#66),
    # so these bytes are effectively a no-op marker on current firmware.
    # Structure: {1: {2: {}, 5: {1: {1: 3, 2: 2, 3: 1}, 5: {}}}}
    #   field 5.1.1 = suction level (3=max)
    #   field 5.1.2 = mop humidity (2=wet)
    #   field 5.1.3 = passes (1=single)
    _DEFAULT_CLEAN_PAYLOAD = bytes.fromhex("0a0e12002a0a0a060803100218012a00")

    async def _all_room_ids(self) -> list[int]:
        """Every cleanable room id on the active map, fetching the map if needed."""
        map_data = self.state.map_data
        if not (map_data and map_data.rooms):
            try:
                map_data = await self.get_map()
            except Exception:  # noqa: BLE001 — best-effort prefetch
                _LOGGER.debug("start(): get_map failed; no room list available")
                map_data = self.state.map_data
        if not (map_data and map_data.rooms):
            return []
        return [r.room_id for r in map_data.rooms if r.room_id > 0]

    async def _start_saved_plan(self) -> CommandResponse:
        """Last-resort start: re-run the plan stored on the robot.

        clean/plan/start is StartWithPlan{planId, mapId} — it ignores the
        payload and re-runs whatever plan the robot already holds, which is
        usually the last room selection rather than the whole house. Only
        reachable when no map rooms are known.
        """
        _LOGGER.warning(
            "start(): no map rooms available; falling back to clean/plan/start, "
            "which re-runs the robot's saved plan rather than cleaning every room"
        )
        return await self.send_command(
            TOPIC_CMD_PLAN_START,
            payload=self._DEFAULT_CLEAN_PAYLOAD,
            timeout=10.0,
        )

    async def start(
        self,
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
    ) -> CommandResponse:
        """Start a whole-house clean.

        Enumerates every room on the active map and issues clean/start_clean,
        matching the app's allRoomIds() path.

        The old implementation sent a minimal payload to clean/plan/start and
        returned as soon as the result was anything but NOT_APPLICABLE. Newer
        firmware answers SUCCESS there and does nothing, so the caller saw a
        successful start that never happened (#69). clean/plan/start is a
        plan-runner that discards payloads (#66), so it survives only as a
        last-resort fallback when no map rooms are known.
        """
        room_ids = await self._all_room_ids()
        if not room_ids:
            return await self._start_saved_plan()
        _LOGGER.info("start(): whole-house clean over %d rooms", len(room_ids))
        return await self.start_rooms(
            room_ids,
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
        )

    def _build_clean_payload_v2(
        self,
        room_ids: list[int],
        suction: int = 3,
        mop_humidity: int = 2,
        passes: int = 1,
        clean_mode: int = 3,
    ) -> bytes:
        """Build clean task payload using the v2 schema (firmware v01.07.22+).

        Observed in issue #36 from a Flow on firmware v01.07.22.00.
        Each room entry uses a nested room_id:

            {
              1: {1: 1, 2: <room_id>},                # nested room ref
              2: {1: <suction>, 2: <clean_mode>,      # per-room params
                  3: <passes>, 7: <mop_humidity>},
              3: <sequence>,                          # 1-indexed
            }

        Outer envelope:
            {1: {1: 1, 2: [<rooms>], 3: {}, 5: 6}}

        Defaults match a normal whole-house clean: max Flow 1 suction (3),
        sweep+mop (3 in v2 schema), single pass, wet mop.

        Args:
            room_ids: List of room IDs from RoomInfo.room_id.
            suction: 1-3 (Flow 1) / 1-4 (Flow 2). Default 3 = max for Flow 1.
            mop_humidity: 1=dry, 2=wet, 3=very wet. Default 2.
            passes: Number of passes. Default 1.
            clean_mode: v2 schema cleanMode (3 observed for sweep+mop).

        Returns:
            Encoded protobuf bytes for clean/plan/start.
        """
        import blackboxprotobuf

        room_entries = [
            {
                "1": {"1": 1, "2": rid},
                "2": {
                    "1": suction,
                    "2": clean_mode,
                    "3": passes,
                    "7": mop_humidity,
                },
                "3": idx + 1,
            }
            for idx, rid in enumerate(room_ids)
        ]

        room_entry_typedef = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "uint"},
                    },
                },
                "2": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "int"},
                        "3": {"type": "int"},
                        "7": {"type": "int"},
                    },
                },
                "3": {"type": "int"},
            },
        }

        msg = {
            "1": {
                "1": 1,
                "2": room_entries if len(room_entries) > 1 else room_entries[0],
                "3": {},
                "5": 6,
            }
        }
        typedef = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "int"},
                    "2": room_entry_typedef,
                    "3": {"type": "message", "message_typedef": {}},
                    "5": {"type": "int"},
                },
            }
        }
        return blackboxprotobuf.encode_message(msg, typedef)

    # WorkMode -> (CleanParam.mode tag 1, pass-count tags to set from `passes`). The robot's
    # execution mode is CleanTask.taskType (= the WorkMode value); CleanParam.mode and the
    # pass tag are derived here so the two can't drift. Live-validated on a Flow 2; see
    # project_history.md "CleanParam — fully decoded".
    _WORK_MODE_PARAM: dict[WorkMode, tuple[int, tuple[str, ...]]] = {
        WorkMode.VACUUM: (2, ("5",)),               # sweepTime
        WorkMode.MOP: (3, ("6",)),                 # mopTime
        WorkMode.VACUUM_THEN_MOP: (5, ("5", "6")),  # sweep + mop pass counts
        WorkMode.VACUUM_AND_MOP: (4, ("7",)),      # sweepMopSyncTime
    }

    def _clean_param_for_settings(self, settings: RoomCleanSettings) -> dict[str, int]:
        """Return a CleanParam protobuf dict for one room."""
        param_mode, pass_tags = self._WORK_MODE_PARAM[settings.work_mode]
        param: dict[str, int] = {
            "1": int(param_mode),
            "2": int(settings.fan),
            "3": int(settings.mop_strength),
            "4": int(settings.water),
        }
        if settings.route is not None:
            param["8"] = int(settings.route)
        for tag in pass_tags:
            param[tag] = int(settings.passes)
        return param

    @staticmethod
    def _task_type_for_settings(
        room_settings: list[RoomCleanSettings],
        fallback: WorkMode,
    ) -> WorkMode:
        """Return the outer CleanTask type for a room-clean payload."""
        modes = {settings.work_mode for settings in room_settings}
        if len(modes) == 1:
            return next(iter(modes))
        if WorkMode.VACUUM_AND_MOP in modes:
            return WorkMode.VACUUM_AND_MOP
        if WorkMode.VACUUM_THEN_MOP in modes:
            return WorkMode.VACUUM_THEN_MOP
        if {WorkMode.VACUUM, WorkMode.MOP}.issubset(modes):
            return WorkMode.VACUUM_THEN_MOP
        return fallback

    def _build_start_clean_payload(
        self,
        room_ids: list[int],
        map_id: int,
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
        route: CleaningRoute | None = None,
        room_settings: Mapping[int, RoomCleanSettings] | None = None,
    ) -> bytes:
        """Build a clean/start_clean request for the given rooms.

        StartClean_Request{1: CleanTask{1: map_id, 2: [CleanItem...], 3: {} (TaskOption),
        5: taskType}}; CleanItem{1: ZoneOption{1: 1 (room zone), 2: room_id}, 2: CleanParam,
        3: order}. taskType (the execution-mode carrier) and CleanParam.mode/pass-tag are
        derived from work_mode. overlapLevel is CleanParam tag 8 when supplied.

        Args:
            room_ids: Robot room IDs (RoomInfo.room_id).
            map_id: Active map id (MapData.map_id, get_map field 2.1).
            work_mode: Vacuum / mop / vacuum-then-mop / vacuum-and-mop.
            fan: Suction level (CleanParam tag 2).
            water: Mop water volume (tag 4).
            mop_strength: Mop scrub intensity (tag 3).
            passes: Clean count, routed to the pass tag(s) for the mode.
            route: Optional route overlap level (tag 8).
            room_settings: Optional per-room settings keyed by robot room ID.
        """
        import blackboxprotobuf

        default_settings = RoomCleanSettings(
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
        )
        settings_by_room = (
            {rid: room_settings.get(rid, default_settings) for rid in room_ids}
            if room_settings
            else {rid: default_settings for rid in room_ids}
        )
        task_type = self._task_type_for_settings(
            list(settings_by_room.values()),
            work_mode,
        )
        param_keys: set[str] = set()
        params_by_room: dict[int, dict[str, int]] = {}
        for rid, settings in settings_by_room.items():
            params_by_room[rid] = self._clean_param_for_settings(settings)
            param_keys.update(params_by_room[rid])

        items = [
            {"1": {"1": 1, "2": rid}, "2": params_by_room[rid], "3": idx + 1}
            for idx, rid in enumerate(room_ids)
        ]
        task = {
            "1": map_id,
            "2": items if len(items) > 1 else items[0],
            "3": {},
            "5": int(task_type),  # CleanTask.taskType
        }
        item_typedef = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {"type": "message", "message_typedef": {
                    "1": {"type": "int"}, "2": {"type": "int"},
                }},
                # Derive the CleanParam typedef from the emitted dict — bbpb silently
                # drops any tag absent from the typedef.
                "2": {"type": "message", "message_typedef": {
                    k: {"type": "int"} for k in sorted(param_keys, key=int)
                }},
                "3": {"type": "int"},
            },
        }
        typedef = {"1": {"type": "message", "message_typedef": {
            "1": {"type": "int"},
            "2": item_typedef,
            "3": {"type": "message", "message_typedef": {}},
            "5": {"type": "int"},
        }}}
        return blackboxprotobuf.encode_message({"1": task}, typedef)

    async def start_rooms(
        self,
        room_ids: list[int],
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
        route: CleaningRoute | None = None,
        room_settings: Mapping[int, RoomCleanSettings] | None = None,
    ) -> CommandResponse:
        """Start cleaning the given rooms via clean/start_clean.

        Room cleaning must use clean/start_clean (StartClean → CleanTask), not
        clean/plan/start: on Flow firmware the latter is StartWithPlan{planId,
        mapId} and ignores any room payload — the root cause of #25/#37, where
        the robot undocks and wanders instead of cleaning the selected rooms.
        The CleanTask carries the active map id (get_map field 2.1).

        clean/start_clean only works while docked; from STANDBY the robot
        returns NOT_READY (4). Callers should start from the dock; this retries
        briefly to cover the dock settling transition.

        Args:
            room_ids: Robot room IDs (RoomInfo.room_id), mapped from HA areas.
            work_mode, fan, water, mop_strength, passes, route: CleanParam settings —
                see _build_start_clean_payload.
            room_settings: Optional per-room settings keyed by robot room ID.
        """
        if not room_ids:
            # No rooms selected — do not call start(), which would resolve the
            # full room list and come straight back here.
            return await self._start_saved_plan()

        map_data = self.state.map_data
        if not map_data or not map_data.map_id:
            try:
                map_data = await self.get_map()
            except (NarwalCommandError, NarwalConnectionError) as err:
                _LOGGER.debug("start_rooms: map query failed before wake: %s", err)
                await self.wake(timeout=10.0, force=True)
                map_data = await self.get_map()
        map_id = map_data.map_id if map_data else 0
        if not map_id:
            _LOGGER.warning(
                "start_rooms: no active map id available; cannot start room clean"
            )
            return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

        payload = self._build_start_clean_payload(
            room_ids, map_id, work_mode=work_mode, fan=fan, water=water,
            mop_strength=mop_strength, passes=passes, route=route,
            room_settings=room_settings,
        )
        resp = await self.send_command(
            TOPIC_CMD_CLEAN_TASK, payload=payload, timeout=10.0,
        )
        for _ in range(3):
            if resp.result_code != CommandResult.NOT_READY:
                break
            if not self.state.is_docked:
                _LOGGER.warning(
                    "start_rooms: robot not docked (status=%s); clean/start_clean "
                    "requires the robot on the dock",
                    self.state.working_status.name,
                )
                break
            _LOGGER.info("start_rooms: robot docking/settling, retrying clean/start_clean")
            await asyncio.sleep(3.0)
            resp = await self.send_command(
                TOPIC_CMD_CLEAN_TASK, payload=payload, timeout=10.0,
            )
        return resp

    async def start_easy_clean(self) -> CommandResponse:
        """Start quick/easy clean."""
        return await self.send_command(TOPIC_CMD_EASY_CLEAN)

    async def pause(self) -> CommandResponse:
        """Pause current task."""
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Resume paused task."""
        return await self.send_command(TOPIC_CMD_RESUME, timeout=timeout)

    async def stop(self, timeout: float = 15.0) -> CommandResponse:
        """Force-stop current task.

        Note: force_end is slow — robot physically stops before responding.
        Previous testing shows 10-15s response times from CLEANING state.
        """
        return await self.send_command(TOPIC_CMD_FORCE_END, timeout=timeout)

    async def cancel(self) -> CommandResponse:
        """Cancel current task."""
        return await self.send_command(TOPIC_CMD_CANCEL)

    def _active_dock_task(self) -> str | None:
        """Return the current dock task from robot-derived state."""
        tasks = self._active_dock_tasks()
        if not tasks:
            return None
        return tasks[0]

    def _active_dock_tasks(self) -> tuple[str, ...]:
        """Return current dock tasks from robot-derived state."""
        state = self.state
        if not state.is_station_active:
            return ()
        tasks: list[str] = []
        if state.station_activity == 1:
            tasks.append("emptying_dustbin")
        if state.is_washing_mop:
            tasks.append("washing_mop")
        tasks.extend(state.active_dock_drying_tasks)
        if state.is_drying_mop and "drying_mop" not in tasks:
            tasks.append("drying_mop")
        if state.station_activity == 4 and not any(
            task.startswith("dry") or task == "drying_mop" for task in tasks
        ):
            tasks.append("drying_or_disinfecting")
        return tuple(dict.fromkeys(tasks)) or ("station_active",)

    async def _refresh_after_dock_stop(self) -> bool:
        """Refresh robot state after a dock stop command."""
        updated = False
        try:
            await self.get_status(full_update=True)
            updated = True
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.debug("Status refresh after dock stop failed: %s", err)
        try:
            await self.get_dry_mop_remain_time()
            updated = True
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.debug("Drying timer refresh after dock stop failed: %s", err)
        return updated

    async def stop_dock_task(self, task: str | None = None) -> CommandResponse:
        """Stop the active station maintenance task."""
        active_tasks = self._active_dock_tasks()
        active_task = task or (active_tasks[0] if active_tasks else None)
        payload = _DOCK_TASK_FORCE_END_PAYLOADS.get(active_task or "")
        if payload is None:
            if len(active_tasks) > 1:
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            response = await self.stop(timeout=15.0)
        else:
            response = await self.send_command(
                TOPIC_CMD_FORCE_END,
                payload=payload,
                timeout=15.0,
            )
        response_seen_at = time.monotonic()
        await asyncio.sleep(_DOCK_TASK_REFRESH_DELAY)
        await self._refresh_after_dock_stop()
        if response.success:
            if (
                active_task in _DOCK_TASK_FORCE_END_PAYLOADS
                and active_task in self._active_dock_tasks()
                and self.state.dock_drying_status_time <= response_seen_at
            ):
                self.state.clear_dock_drying_task(active_task)
            if not self.state.is_station_active:
                self.state.clear_washing_task()
                if self.state.dry_mop_remaining_time in (None, 0):
                    self.state.clear_drying_task()
            elif (
                active_task in _DOCK_TASK_FORCE_END_PAYLOADS
                and active_task in self._active_dock_tasks()
            ):
                _LOGGER.debug("Dock task %s still reported after stop", active_task)
                return CommandResponse(
                    result_code=CommandResult.NOT_APPLICABLE,
                    data=response.data,
                    raw_payload=response.raw_payload,
                )
            elif active_task not in _DOCK_TASK_FORCE_END_PAYLOADS:
                self.state.clear_washing_task()
                if self.state.dry_mop_remaining_time in (None, 0):
                    self.state.clear_drying_task()
        return response

    async def return_to_base(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Return to charging dock."""
        return await self.send_command(TOPIC_CMD_RECALL, timeout=timeout)

    async def set_fan_speed(self, level: FanLevel | int) -> CommandResponse:
        """Set suction fan speed live (clean/set_fan_level, field 1 = SweepFanLevel).

        The live command's enum is SweepFanLevel, which has no SUPER — the app maps
        FanLevel.SUPER -> STRONG here. Ints otherwise match FanLevel (MUTE 1, NORMAL 2,
        STRONG 3, DEEP 4).
        """
        live = FanLevel.STRONG if int(level) == FanLevel.SUPER else int(level)
        payload = b"\x08" + bytes([live & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, payload)

    async def set_mop_humidity(self, level: MopHumidity | int) -> CommandResponse:
        """Set mop water volume live (clean/set_mop_humidity, field 1 = MopHumidity).

        Args:
            level: MopHumidity enum or int (1=dry, 2=normal, 3=wet).
        """
        payload = b"\x08" + bytes([int(level) & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, payload)

    async def wash_mop(self) -> CommandResponse:
        """Wash the mop pads at the station."""
        return await self.send_command(TOPIC_CMD_WASH_MOP)

    async def wash_mop_by_robot_status(self) -> CommandResponse:
        """Wash mop pads using the app's status-gated station command."""
        return await self.send_command(TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS)

    async def dry_mop(self) -> CommandResponse:
        """Dry the mop pads at the station."""
        response = await self.send_command(TOPIC_CMD_DRY_MOP)
        if response.result_code in (0, CommandResult.SUCCESS, CommandResult.APPLIED):
            self.state.last_dry_mop_empty_time = 0.0
        return response

    async def dry_dust_bag(self) -> CommandResponse:
        """Dry or disinfect the robot dust bin at the station."""
        response = await self.send_command(TOPIC_CMD_DRY_DUST_BAG)
        if response.result_code in (0, CommandResult.SUCCESS, CommandResult.APPLIED):
            self.state.assume_dock_drying_task("dry_dust_bin")
        return response

    async def dry_station_bag(self) -> CommandResponse:
        """Dry or disinfect the dock dust bag at the station."""
        response = await self.send_command(TOPIC_CMD_DRY_STATION_BAG)
        if response.result_code in (0, CommandResult.SUCCESS, CommandResult.APPLIED):
            self.state.assume_dock_drying_task("dry_dock_bag")
        return response

    async def empty_dustbin(self) -> CommandResponse:
        """Empty the dustbin at the station."""
        return await self.send_command(TOPIC_CMD_DUST_GATHERING)

    # --- Query commands ---

    async def get_device_info(self) -> DeviceInfo:
        """Query device identity (product key, device ID, firmware)."""
        resp = await self.send_command(TOPIC_CMD_GET_DEVICE_INFO)
        data = resp.data

        def _clean_bytes(val: Any) -> str:
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace").rstrip("\n")
            s = str(val)
            if s.startswith("b'") and s.endswith("'"):
                s = s[2:-1]
            return s.rstrip("\n")

        info = DeviceInfo(
            product_key=_clean_bytes(data.get("1", "")),
            device_id=_clean_bytes(data.get("2", "")),
            firmware_version=_clean_bytes(data.get("3", "")),
        )
        self.state.device_info = info
        self.state.firmware_version = info.firmware_version

        # Update topic prefix to match this device's product key
        if info.product_key:
            self.topic_prefix = f"/{info.product_key}"
            _LOGGER.info("Topic prefix set to %s", self.topic_prefix)

        return info

    async def get_feature_list(self) -> dict[int, int]:
        """Query supported features. Returns {feature_id: value}."""
        resp = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST)
        return {int(k): int(v) for k, v in resp.data.items()}

    async def get_status(self, full_update: bool = True) -> CommandResponse:
        """Query current device base status.

        Args:
            full_update: If True, update all state fields (working_status,
                battery, etc). If False, only update hardware-sampled fields
                (battery, health) — used when robot is not broadcasting and
                working_status in the response may be stale.
        """
        resp = await self.send_command(TOPIC_CMD_GET_BASE_STATUS)
        status_data = resp.data.get("2", {})
        if status_data:
            # Log the whole field map, not a chosen few: field-level bug reports
            # (wrong tank state, a value we don't map) are unanswerable from a log
            # that omits the field in question — see #77.
            _LOGGER.debug(
                "get_status response (full=%s): field3=%r, field2=%r, all_fields=%r",
                full_update,
                status_data.get("3") if isinstance(status_data, dict) else None,
                status_data.get("2") if isinstance(status_data, dict) else None,
                status_data,
            )
            if full_update:
                self.state.update_from_base_status(status_data)
            else:
                self.state.update_battery_from_base_status(status_data)
        else:
            _LOGGER.debug("get_status response has no field 2; keys: %s", list(resp.data.keys()))
        return resp

    async def get_current_task(self) -> CommandResponse:
        """Query the current clean task."""
        return await self.send_command(TOPIC_CMD_GET_CURRENT_TASK)

    async def get_consumable_info(self) -> CommandResponse:
        """Query consumable maintain/replace alert lists (not broadcast)."""
        resp = await self.send_command(TOPIC_CMD_GET_CONSUMABLE_INFO, timeout=15.0)
        if resp.success:
            self.state.update_from_consumable_info(resp.data)
        else:
            _LOGGER.debug(
                "Consumable info query failed; preserving existing alerts (code=%s)",
                resp.result_code,
            )
        return resp

    async def reset_consumable_info(
        self,
        *,
        maintain_items: Iterable[int] = (),
        replace_items: Iterable[int] = (),
    ) -> CommandResponse:
        """Clear robot-reported consumable maintenance/replacement alerts."""
        maintain = tuple(int(item) for item in maintain_items)
        replace = tuple(int(item) for item in replace_items)
        payload = self._build_reset_consumable_info_payload(
            maintain_items=maintain,
            replace_items=replace,
        )
        response = await self.send_command(
            TOPIC_CMD_RESET_CONSUMABLE_INFO,
            payload,
            timeout=15.0,
        )
        refresh_response: CommandResponse | None = None
        if response.success:
            refresh_response = await self.get_consumable_info()

        refresh_verified = refresh_response is None or refresh_response.success
        target_still_reported = (
            refresh_verified
            and bool(
                set(maintain).intersection(self.state.maintain_items)
                or set(replace).intersection(self.state.replace_items)
            )
        )
        if payload and target_still_reported:
            _LOGGER.debug(
                "Consumable reset target still reported after targeted clear: "
                "maintain=%s replace=%s",
                maintain,
                replace,
            )
            return CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE,
                data=response.data,
                raw_payload=response.raw_payload,
            )
        return response

    async def get_clean_progress_info(self) -> CommandResponse:
        """Query active clean progress information."""
        resp = await self.send_command(
            TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
            timeout=_OPTIONAL_TASK_DETAIL_TIMEOUT,
            wait_if_busy=False,
        )
        if resp.success:
            self.state.update_from_aux_status(
                TOPIC_CMD_GET_CLEAN_PROGRESS_INFO,
                resp.data,
            )
        else:
            _LOGGER.debug(
                "Clean progress query failed; preserving existing task details (code=%s)",
                resp.result_code,
            )
        return resp

    async def get_dry_mop_remain_time(self) -> CommandResponse:
        """Query remaining mop drying time."""
        resp = await self.send_command(
            TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME,
            timeout=_OPTIONAL_TASK_DETAIL_TIMEOUT,
            wait_if_busy=False,
        )
        if resp.success:
            self.state.update_from_aux_status(
                TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME,
                resp.data,
            )
        else:
            _LOGGER.debug(
                "Dry mop time query failed; preserving existing dock task state (code=%s)",
                resp.result_code,
            )
        return resp

    async def get_robot_task_status(self) -> CommandResponse:
        """Query the robot task status model."""
        resp = await self.send_command(
            TOPIC_CMD_GET_ROBOT_TASK_STATUS,
            timeout=_OPTIONAL_TASK_DETAIL_TIMEOUT,
            wait_if_busy=False,
        )
        if resp.success:
            self.state.update_from_aux_status(TOPIC_CMD_GET_ROBOT_TASK_STATUS, resp.data)
        else:
            _LOGGER.debug(
                "Robot task status query failed; preserving existing task details (code=%s)",
                resp.result_code,
            )
        return resp

    async def get_map(self) -> MapData:
        """Download the full map data."""
        resp = await self.send_command(TOPIC_CMD_GET_MAP, timeout=15.0)
        if not resp.success:
            _LOGGER.debug(
                "Map query failed; preserving existing map data (code=%s)",
                resp.result_code,
            )
            if self.state.map_data is not None:
                return self.state.map_data
            raise NarwalCommandError(f"Map query failed with code {resp.result_code}")
        map_data = await asyncio.to_thread(MapData.from_response, resp.data)
        self.state.map_data = map_data
        return map_data

    async def get_all_maps(self) -> CommandResponse:
        """Download all saved/reduced maps."""
        return await self.send_command(TOPIC_CMD_GET_ALL_MAPS, timeout=15.0)

    async def take_picture(self) -> bytes | None:
        """Capture a photo from the robot's camera.

        Returns raw image bytes from field 2 of the response, or None on failure.
        Note: the image is AES-encrypted; decoding requires the APK-derived key
        which is not yet known. Callers receive raw bytes as-is.
        """
        try:
            resp = await self.send_command(TOPIC_CMD_TAKE_PICTURE, timeout=15.0)
        except Exception:
            _LOGGER.warning("take_picture command failed")
            return None
        if resp.success:
            return resp.data.get("2")
        _LOGGER.warning("take_picture returned result_code=%d", resp.result_code)
        return None

    async def get_robot_debug_image(self) -> bytes | None:
        """Fetch the robot's latest debug/planning image (cleartext PNG).

        developer/get_robot_debug_image returns a batch of carpet-detection and
        planning overlays as UNENCRYPTED PNGs (222x232 RGB), keyed by filename
        (InitCarpet/GlobalCarpet/UpdateRoomN_PlanningCarpet). Only produced while
        the robot is mapping/cleaning; returns None when idle or unavailable.

        Response shape: field 1 = repeated {1: filename, 2: png_bytes}. Returns the
        most recent whole-map ("GlobalCarpet") PNG if present, else the last image
        in the batch, else None.
        """
        try:
            resp = await self.send_command(TOPIC_CMD_GET_DEBUG_IMAGE, timeout=15.0)
        except Exception:
            _LOGGER.warning("get_robot_debug_image command failed")
            return None
        entries = resp.data.get("1")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return None
        best: bytes | None = None
        last: bytes | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            img = entry.get("2")
            if not isinstance(img, (bytes, bytearray)):
                continue
            last = bytes(img)
            name = entry.get("1")
            if isinstance(name, str) and "GlobalCarpet" in name:
                best = bytes(img)  # prefer the most recent whole-map carpet overlay
        return best or last

    async def set_led(self, on: bool) -> None:
        """Turn the camera LED fill light on or off.

        Payload: 0x08 0x01 = on, 0x08 0x00 = off (protobuf field 1, varint).
        """
        payload = b"\x08\x01" if on else b"\x08\x00"
        try:
            resp = await self.send_command(TOPIC_CMD_SET_LED, payload=payload)
        except Exception:
            _LOGGER.warning("set_led(%s) command failed", on)
            return
        if resp.result_code not in (CommandResult.SUCCESS, CommandResult.NOT_APPLICABLE):
            _LOGGER.warning(
                "set_led(%s) unexpected result_code=%d", on, resp.result_code
            )

    async def set_ambient_light_mode(
        self, mode: AmbientLightCtrlType | int
    ) -> CommandResponse | None:
        """Set the base station ambient light mode."""
        mode = AmbientLightCtrlType(mode)
        payload = self._encode_varint_field(1, int(mode))
        try:
            resp = await self.send_command(
                TOPIC_CMD_AMBIENT_LIGHT_CTRL,
                payload=payload,
            )
        except Exception:
            _LOGGER.warning("set_ambient_light_mode(%s) command failed", mode)
            return None
        if resp.result_code not in (
            0,
            CommandResult.SUCCESS,
            CommandResult.NOT_APPLICABLE,
            CommandResult.APPLIED,
        ):
            _LOGGER.warning(
                "set_ambient_light_mode(%s) unexpected result_code=%d",
                mode,
                resp.result_code,
            )
        return resp
