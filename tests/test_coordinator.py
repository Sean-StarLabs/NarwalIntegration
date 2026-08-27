"""Tests for NarwalCoordinator resilience -- failure buffering and push reset.

Verifies the coordinator returns stale data on transient failures, raises
UpdateFailed after the threshold, and resets counters on success/push.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.const import NO_BROADCAST_PRODUCT_KEYS  # noqa: E402
from custom_components.narwal.coordinator import (  # noqa: E402
    TOPIC_RESUBSCRIBE_AFTER,
    TOPIC_SUBSCRIPTION_TTL,
    CleanSettings,
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_start_cleaning,
    is_live_clean_setting_available,
)  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MapData,
    MapDisplayData,
    MopHumidity,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkingStatus,
    WorkMode,
)

UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


class _FakeStore:
    """Store test double that records saved payloads."""

    def __init__(self, data: object | None = None) -> None:
        self.data = data
        self.saved: list[object] = []

    async def async_load(self) -> object | None:
        return self.data

    async def async_save(self, data: object) -> None:
        self.saved.append(data)
        self.data = data


def _trajectory_state() -> NarwalState:
    """Return a state with static map and native display-map trajectory."""
    state = NarwalState()
    state.map_data = MapData(
        map_id=12,
        width=100,
        height=100,
        created_at=34,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(
        robot_x=1.25,
        robot_y=2.5,
        robot_heading=90.0,
        timestamp=123456,
        dock_ref_x=3.0,
        dock_ref_y=4.0,
        trajectory_x_values=b"xxxx",
        trajectory_y_values=b"yyyy",
        trajectory_signature=(4, 4, 99),
    )
    return state


def _close_background_task(_hass: object, coro: object, _name: str) -> MagicMock:
    """Consume scheduled coroutine objects in coordinator unit tests."""
    close = getattr(coro, "close", None)
    if close is not None:
        close()
    task = MagicMock()
    task.done.return_value = True
    return task


def test_non_broadcast_product_key_configures_polling_client() -> None:
    """Coordinator propagates the product capability to the client."""
    product_key = next(iter(NO_BROADCAST_PRODUCT_KEYS))
    entry = MagicMock()
    entry.data = {
        "host": "10.0.0.70",
        "port": 9002,
        "device_id": "device-id",
        "product_key": product_key,
    }

    with patch("custom_components.narwal.coordinator.NarwalClient") as client_class:
        NarwalCoordinator(MagicMock(), entry)

    client_class.assert_called_once_with(
        host="10.0.0.70",
        port=9002,
        device_id="device-id",
        topic_prefix=f"/{product_key}",
        supports_broadcasts=False,
    )


def test_room_profiles_only_override_customized_fields() -> None:
    """Read-only room profile creation must not freeze global defaults."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}

    coordinator.room_clean_settings_for(4)
    coordinator.clean_settings.fan = FanLevel.STRONG
    coordinator.clean_settings.water = MopHumidity.WET

    settings = coordinator.room_clean_settings_for_rooms([4])[4]

    assert settings.fan == FanLevel.STRONG
    assert settings.water == MopHumidity.WET

    coordinator.set_room_clean_setting(4, "water", MopHumidity.DRY)
    merged = coordinator.room_clean_settings_for_rooms([4])[4]

    assert merged.fan == FanLevel.STRONG
    assert merged.water == MopHumidity.DRY


def test_room_profile_override_can_be_cleared() -> None:
    """Cleared fields should fall back to the current global clean setting."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}

    coordinator.set_room_clean_setting(4, "water", MopHumidity.DRY)
    coordinator.clean_settings.water = MopHumidity.WET

    coordinator.clear_room_clean_setting(4, "water")
    settings = coordinator.room_clean_settings_for_rooms([4])[4]

    assert settings.water == MopHumidity.WET
    assert coordinator.room_clean_settings_customized == {}


def test_effective_room_profile_follows_global_defaults_until_customized() -> None:
    """Room entity reads should not materialize stale inherited defaults."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}

    first = coordinator.effective_room_clean_settings_for(4)
    coordinator.clean_settings.route = first.route
    coordinator.clean_settings.fan = FanLevel.STRONG

    inherited = coordinator.effective_room_clean_settings_for(4)

    assert inherited.fan == FanLevel.STRONG
    assert coordinator.room_clean_settings == {}

    coordinator.set_room_clean_setting(4, "water", MopHumidity.DRY)
    coordinator.clean_settings.water = MopHumidity.WET
    customized = coordinator.effective_room_clean_settings_for(4)

    assert customized.fan == FanLevel.STRONG
    assert customized.water == MopHumidity.DRY


def test_room_profiles_can_be_bypassed_for_explicit_service_settings() -> None:
    """Callers can request exact settings without saved room-profile overrides."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.set_room_clean_setting(4, "fan", FanLevel.MUTE)
    requested = RoomCleanSettings(fan=FanLevel.STRONG)

    settings = coordinator.room_clean_settings_for_rooms(
        [4],
        default=requested,
        use_room_profiles=False,
    )[4]

    assert settings is requested
    assert settings.fan == FanLevel.STRONG


def test_selected_clean_rooms_fall_back_to_all_rooms() -> None:
    """No room selection means the native start command cleans every room."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {}

    assert coordinator.selected_clean_room_ids_for([4, 5]) == [4, 5]


def test_selected_clean_rooms_are_filtered_and_map_scoped() -> None:
    """Room selections only apply to the active map and known room IDs."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {}

    coordinator.set_room_selected_for_clean(5, True, map_id="upstairs")
    coordinator.set_room_selected_for_clean(99, True, map_id="upstairs")

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == [5]
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="downstairs") == [4, 5]

    coordinator.set_room_selected_for_clean(5, False, map_id="upstairs")
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == [4, 5]


def test_paused_standby_task_context_blocks_new_actions() -> None:
    """Paused STANDBY overlays still represent the current clean task."""
    state = NarwalState()
    state.task_progress_percent = 72
    state.task_elapsed_time = 900
    state.current_room_id = 4

    state.update_from_base_status({"3": {"1": 1, "2": 1}, "11": 1, "47": 2})

    assert state.working_status == WorkingStatus.STANDBY
    assert state.is_paused
    assert state.has_paused_clean_task_context
    assert is_live_clean_setting_available(state)
    assert not can_edit_pending_clean_settings(state)
    assert not can_start_cleaning(state)


def test_map_display_cache_payload_round_trips_native_trajectory() -> None:
    """Native display-map trails can be serialized through HA storage."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = _trajectory_state()

    payload = coordinator._map_display_cache_payload(state)
    restored = NarwalCoordinator._map_display_from_cache(payload)

    assert payload is not None
    assert restored is not None
    assert payload["map_id"] == 12
    assert payload["map_created_at"] == 34
    assert payload["active_clean"] is False
    assert restored.robot_x == 0.0
    assert restored.robot_y == 0.0
    assert restored.robot_heading == 0.0
    assert restored.timestamp == state.map_display_data.timestamp
    assert restored.dock_ref_x == state.map_display_data.dock_ref_x
    assert restored.dock_ref_y == state.map_display_data.dock_ref_y
    assert restored.trajectory_x_values == b"xxxx"
    assert restored.trajectory_y_values == b"yyyy"
    assert restored.trajectory_signature == (4, 4, 99)


async def test_restore_map_display_cache_restores_matching_static_map() -> None:
    """A saved trail is restored after restart when the static map matches."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.client.state.map_data = source.map_data
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    restored = coordinator.client.state.map_display_data
    assert restored is not None
    assert restored.trajectory_signature == (4, 4, 99)
    assert coordinator._map_display_cache_signature == (4, 4, 99)
    assert coordinator._map_display_cache_restored


async def test_restore_map_display_cache_does_not_overwrite_live_trail() -> None:
    """A live display_map packet received during startup wins over stored routes."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    cached = _trajectory_state()
    cached.map_display_data = MapDisplayData(
        robot_x=5.0,
        robot_y=6.0,
        robot_heading=180.0,
        timestamp=123457,
        dock_ref_x=3.0,
        dock_ref_y=4.0,
        trajectory_x_values=b"aa",
        trajectory_y_values=b"bb",
        trajectory_signature=(2, 2, 77),
    )
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(cached)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data.trajectory_signature == (4, 4, 99)
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored


async def test_restore_map_display_cache_waits_for_static_map() -> None:
    """A saved trail is not restored until the active static map is known."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._pending_map_display_cache_restore is not None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored

    coordinator.client.state.map_data = source.map_data
    coordinator._restore_pending_map_display_cache()

    restored = coordinator.client.state.map_display_data
    assert restored is not None
    assert restored.trajectory_signature == (4, 4, 99)
    assert coordinator._pending_map_display_cache_restore is None
    assert coordinator._map_display_cache_restored


async def test_restore_map_display_cache_ignores_different_static_map() -> None:
    """A saved trail from another map must not be overlaid."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.client.state.map_data = MapData(
        map_id=13,
        width=100,
        height=100,
        created_at=34,
        compressed_map=b"\x01",
    )
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored


async def test_clear_map_display_cache_clears_memory_and_store() -> None:
    """Accepted clean starts clear both memory and persisted trail state."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = (
        coordinator._map_display_cache_snapshot(coordinator.client.state)
    )
    coordinator._pending_map_display_cache_restore = {"pending": "trail"}
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_save_task = asyncio.create_task(asyncio.sleep(60))

    await coordinator.async_clear_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._pending_map_display_cache_snapshot is None
    assert coordinator._pending_map_display_cache_restore is None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored
    assert coordinator._map_display_cache_save_task is None
    assert coordinator._map_display_cache_store.saved == [{}]


def test_reconnect_into_running_clean_keeps_restored_trail() -> None:
    """First cleaning update after restoring a cache is not a new clean start."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert not coordinator._is_new_clean_transition(state)


def test_unknown_to_cleaning_clears_inactive_restored_trail() -> None:
    """A completed cached trail must not be treated as an active reconnect."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert coordinator._is_new_clean_transition(state)


def test_idle_to_cleaning_transition_clears_stale_restored_trail() -> None:
    """A clean started while HA is running must drop the previous clean's trail."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator.config_entry.async_create_background_task.side_effect = (
        _close_background_task
    )

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert coordinator._is_new_clean_transition(state)
    coordinator._clear_map_display_cache_for_new_clean(state)

    assert state.map_display_data is None
    assert coordinator._map_display_cache_signature == ()
    coordinator.config_entry.async_create_background_task.assert_called_once()


def test_new_clean_with_fresh_native_trail_replaces_cached_trail() -> None:
    """Fresh display-map data at clean start should be persisted, not cleared."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = 1.0
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (1, 1, 1)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = time.monotonic()
    coordinator.config_entry.async_create_background_task.side_effect = (
        _close_background_task
    )

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    coordinator._clear_map_display_cache_for_new_clean(state)

    assert state.map_display_data is not None
    assert coordinator._pending_map_display_cache_snapshot is not None
    assert (
        coordinator._pending_map_display_cache_snapshot.trajectory_signature
        == (4, 4, 99)
    )
    assert coordinator._map_display_cache_store.saved == []
    coordinator.config_entry.async_create_background_task.assert_called_once()


def test_schedule_map_display_cache_save_defers_serialization() -> None:
    """Scheduling cache persistence must not encode full routes in callbacks."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock(
        side_effect=_close_background_task
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = time.monotonic()

    with patch.object(
        NarwalCoordinator,
        "_map_display_cache_payload_from_snapshot",
        side_effect=AssertionError("serialization should be throttled"),
    ):
        coordinator._schedule_map_display_cache_save(_trajectory_state())

    assert coordinator._pending_map_display_cache_snapshot is not None
    coordinator.config_entry.async_create_background_task.assert_called_once()


async def test_shutdown_flushes_current_display_map_cache() -> None:
    """HA shutdown should persist the newest trail even when no save is queued."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_signature = ()
    coordinator._map_display_cache_last_save = 0.0

    await coordinator._async_flush_map_display_cache()

    assert coordinator._map_display_cache_store.saved
    assert coordinator._map_display_cache_store.saved[-1]["trajectory_signature"] == [
        4,
        4,
        99,
    ]
    assert coordinator._map_display_cache_signature == (4, 4, 99)


class TestCoordinatorResilience:
    """Tests for NarwalCoordinator failure buffering and availability."""

    def _make_coordinator(self) -> NarwalCoordinator:
        """Create a NarwalCoordinator with mocked hass and entry."""
        mock_hass = MagicMock()
        mock_entry = MagicMock()
        mock_entry.data = {
            "host": "10.0.0.100",
            "port": 9002,
            "device_id": "test_device",
            "product_key": "QoEsI5qYXO",
        }

        coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
        # Initialize the attributes that __init__ sets, bypassing
        # DataUpdateCoordinator.__init__ which needs a real hass.
        coordinator.hass = mock_hass
        coordinator.config_entry = mock_entry
        coordinator.client = MagicMock()
        coordinator.client.state = NarwalState()
        coordinator.last_update_success = True
        coordinator._consecutive_failures = 0
        coordinator._dock_status_refresh_failed = False
        coordinator._max_failures = 5
        coordinator._consumable_poll_countdown = 99  # don't fire consumable poll in unit tests
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator._last_display_map_resub = 0.0
        # Fresh subscription so renewal does not fire in unrelated tests.
        coordinator._last_topic_subscribe = time.monotonic()
        coordinator._prev_working_status = MagicMock()
        coordinator._map_display_cache_store = _FakeStore()
        coordinator._map_display_cache_signature = ()
        coordinator._pending_map_display_cache_snapshot = None
        coordinator._pending_map_display_cache_restore = None
        coordinator._map_display_cache_save_task = None
        coordinator._map_display_cache_last_save = 0.0
        coordinator._map_display_cache_restored = False
        coordinator.update_interval = None
        # Prevent background task warnings
        mock_entry.async_create_background_task = MagicMock(
            side_effect=_close_background_task
        )
        return coordinator

    async def test_stale_data_on_first_failure(self) -> None:
        """_async_update_data returns stale state on first poll failure."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1

    async def test_stale_data_on_consecutive_failures_below_threshold(self) -> None:
        """_async_update_data returns stale state for failures 1-4."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        for i in range(4):
            result = await coordinator._async_update_data()
            assert result is coordinator.client.state
            assert coordinator._consecutive_failures == i + 1

    async def test_update_failed_after_max_failures(self) -> None:
        """_async_update_data raises UpdateFailed after 5 consecutive failures."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        # Burn through 4 failures (stale data returned)
        for _ in range(4):
            await coordinator._async_update_data()

        # 5th failure raises UpdateFailed
        with pytest.raises(UpdateFailed, match="5 consecutive polls"):
            await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 5

    async def test_success_resets_failure_counter(self) -> None:
        """_async_update_data resets _consecutive_failures to 0 on success."""
        coordinator = self._make_coordinator()

        # Simulate 3 failures first
        type(coordinator.client).connected = PropertyMock(return_value=False)
        for _ in range(3):
            await coordinator._async_update_data()
        assert coordinator._consecutive_failures == 3

        # Now succeed
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )

        result = await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 0
        assert result is coordinator.client.state

    async def test_poll_preserves_recent_active_working_status(self) -> None:
        """Poll only refreshes hardware fields while task metrics are fresh."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state.update_from_working_status({"3": 120})
        coordinator.client.get_status = AsyncMock()

        result = await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        assert result is coordinator.client.state

    async def test_poll_clean_transition_clears_stale_restored_trail(self) -> None:
        """Polling must clear a previous clean's trail when push updates are absent."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state = _trajectory_state()
        coordinator.client.state.working_status = WorkingStatus.STANDBY
        coordinator.client.last_display_map_age = float("inf")
        coordinator._prev_working_status = WorkingStatus.STANDBY
        coordinator._map_display_cache_signature = (4, 4, 99)
        coordinator._map_display_cache_restored = True
        coordinator._map_display_cache_restored_from_active = False

        async def get_status(*, full_update: bool) -> CommandResponse:
            coordinator.client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.CLEANING)}}
            )
            return CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}}
            )

        coordinator.client.get_status = AsyncMock(side_effect=get_status)

        result = await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        assert result is coordinator.client.state
        assert result.map_display_data is None
        assert coordinator._map_display_cache_signature == ()
        assert coordinator._prev_working_status == WorkingStatus.CLEANING
        coordinator.config_entry.async_create_background_task.assert_called_once()

    async def test_push_update_resets_failure_counter(self) -> None:
        """_on_state_update resets _consecutive_failures to 0."""
        coordinator = self._make_coordinator()
        coordinator._consecutive_failures = 3
        coordinator._dock_status_refresh_failed = True

        # Mock methods called by _on_state_update
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = MagicMock()

        state = NarwalState()
        coordinator._on_state_update(state)

        assert coordinator._consecutive_failures == 0
        assert not coordinator.has_fresh_state

    async def test_idle_push_clears_active_clean_work_mode(self) -> None:
        """Accepted-task mode metadata is only kept for active clean contexts."""
        coordinator = self._make_coordinator()
        coordinator.active_clean_work_mode = WorkMode.MOP
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = WorkingStatus.CLEANING
        state = NarwalState()
        state.update_from_base_status({"3": {"1": int(WorkingStatus.DOCKED), "3": 6}})

        coordinator._on_state_update(state)

        assert coordinator.active_clean_work_mode is None

    async def test_poll_does_not_call_connect(self) -> None:
        """_async_update_data does NOT call client.connect() when disconnected."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)
        coordinator.client.connect = AsyncMock()

        # Run a few poll failures
        for _ in range(3):
            await coordinator._async_update_data()

        coordinator.client.connect.assert_not_awaited()

    async def test_connected_but_get_status_fails(self) -> None:
        """_async_update_data buffers failure when connected but get_status raises."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            side_effect=NarwalConnectionError("recv timeout")
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1
        assert not coordinator.has_fresh_state

    async def test_payloadless_status_poll_counts_as_failed_refresh(self) -> None:
        """A status ack without base-status data must not mark stale data fresh."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                result_code=CommandResult.NOT_READY,
                data={"1": 1},
            )
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1

    async def test_partial_status_poll_counts_as_failed_refresh(self) -> None:
        """A battery-only base-status response is not fresh dock-state data."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1

    async def test_refresh_dock_status_rejects_missing_base_status_payload(self) -> None:
        """Dock command gates require fresh base-status data, not only an ack."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )

    async def test_refresh_dock_status_rejects_rejected_status_response(self) -> None:
        """Rejected status responses must not unlock dock controls."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE,
                data={"2": {"3": {"1": 10}}},
            )
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_rejects_partial_base_status_payload(self) -> None:
        """Dock command gates require field 3, not just any base-status field."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_rejects_empty_dock_status_payload(self) -> None:
        """Dock command gates require real status subfields inside field 3."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {}}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_marks_fresh_before_notifying(self) -> None:
        """Listeners see fresh dock availability on the successful refresh update."""
        coordinator = self._make_coordinator()
        coordinator._dock_status_refresh_failed = True
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}}
            )
        )
        seen: list[bool] = []

        def capture_update(_state):
            seen.append(coordinator.has_fresh_state)

        coordinator.async_set_updated_data = MagicMock(side_effect=capture_update)

        assert await coordinator.async_refresh_dock_status()
        assert seen == [True]

    async def test_refresh_dock_status_preserves_live_working_status(self) -> None:
        """Action preflight must not clobber fresh working_status task telemetry."""
        coordinator = self._make_coordinator()
        coordinator.client.state.update_from_working_status({"3": 42})
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert await coordinator.async_refresh_dock_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        assert not coordinator._dock_status_refresh_failed

    async def test_refresh_dock_status_marks_stale_before_notifying(self) -> None:
        """Listeners see stale dock availability on the failed refresh update."""
        coordinator = self._make_coordinator()
        coordinator._dock_status_refresh_failed = False
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        seen: list[bool] = []

        def capture_update(_state):
            seen.append(coordinator.has_fresh_state)

        coordinator.async_set_updated_data = MagicMock(side_effect=capture_update)

        assert not await coordinator.async_refresh_dock_status()
        assert seen == [False]

    async def test_action_refresh_preserves_recent_active_working_status(self) -> None:
        """Robot action gates avoid full base-status refresh while task data is fresh."""
        coordinator = self._make_coordinator()
        coordinator.client.state.update_from_working_status({"3": 120})
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert await coordinator.async_refresh_action_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )
        assert not coordinator._dock_status_refresh_failed

    async def test_action_refresh_requires_dock_payload_when_full_update_needed(self) -> None:
        """Without active task telemetry, action refresh needs real dock/base status."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_action_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        assert coordinator._dock_status_refresh_failed


class TestTopicSubscriptionRenewal:
    """The broadcast subscription must be renewed before it lapses (#73).

    The robot only broadcasts status/working_status and display_map while an
    active_robot_publish subscription is live, and that lasts 600 s. Observed on
    hardware 2026-08-08 during a real room clean: with the subscription expired,
    a 4000-line window carried 423 base_status broadcasts but exactly 1
    working_status and 1 display_map. The vacuum entity sat at "docked" while the
    robot was audibly cleaning, and the live map never moved. Re-subscribing
    turned it straight back on — 211 / 30 / 30 over the next window.

    The renewal must not be conditional on believing we are cleaning: working_status
    is the signal that tells us we are cleaning, so gating renewal on it deadlocks.
    """

    def _coordinator(self, last_subscribe: float) -> NarwalCoordinator:
        c = NarwalCoordinator.__new__(NarwalCoordinator)
        c.hass = MagicMock()
        c.config_entry = MagicMock()
        c.client = MagicMock()
        c.client.state = NarwalState()
        c.client.connected = True
        c.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )
        c.client.get_map = AsyncMock()
        c.client.get_consumable_info = AsyncMock()
        c.client.subscribe_to_topics = AsyncMock()
        c.client.supports_broadcasts = True
        c.client.state.map_data = MagicMock()  # skip the map-retry branch
        c._consecutive_failures = 0
        c._max_failures = 5
        c._consumable_poll_countdown = 99
        c._fast_poll_remaining = 0
        c._listen_task = None
        c._map_fetch_pending = False
        c._last_display_map_resub = 0.0
        c._last_topic_subscribe = last_subscribe
        c._prev_working_status = MagicMock()
        c.update_interval = None
        return c

    @pytest.mark.asyncio
    async def test_renews_when_subscription_is_stale(self) -> None:
        """A poll past the renewal window re-sends the subscription."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        await c._async_update_data()
        c.client.subscribe_to_topics.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_renew_while_subscription_is_fresh(self) -> None:
        """A fresh subscription is not re-sent on every poll."""
        c = self._coordinator(time.monotonic())
        await c._async_update_data()
        c.client.subscribe_to_topics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_broadcast_model_does_not_subscribe(self) -> None:
        """Polling-only models never renew an unsupported broadcast subscription."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.supports_broadcasts = False

        await c._async_update_data()

        c.client.subscribe_to_topics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renewal_is_not_gated_on_cleaning_state(self) -> None:
        """Renewal happens even when the entity believes the robot is docked.

        This is the deadlock that caused #73: no subscription means no
        working_status, which means the entity never leaves "docked", which — if
        renewal were gated on cleaning — would mean the subscription is never
        renewed.
        """
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert c.client.state.is_docked
        assert not c.client.state.is_cleaning

        await c._async_update_data()

        c.client.subscribe_to_topics.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_renewal_window_is_inside_the_ttl(self) -> None:
        """Renew with margin — renewing at or after expiry would still drop frames."""
        assert TOPIC_RESUBSCRIBE_AFTER < TOPIC_SUBSCRIPTION_TTL
        assert TOPIC_RESUBSCRIBE_AFTER <= TOPIC_SUBSCRIPTION_TTL / 2

    @pytest.mark.asyncio
    async def test_renewal_failure_does_not_break_the_poll(self) -> None:
        """A failed renewal must not take the whole update down."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.subscribe_to_topics = AsyncMock(side_effect=RuntimeError("ws closed"))
        state = await c._async_update_data()
        assert state is c.client.state
