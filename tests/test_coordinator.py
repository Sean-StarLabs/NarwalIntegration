"""Tests for NarwalCoordinator resilience -- failure buffering and push reset.

Verifies the coordinator returns stale data on transient failures, raises
UpdateFailed after the threshold, and resets counters on success/push.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.const import NO_BROADCAST_PRODUCT_KEYS  # noqa: E402
from custom_components.narwal.coordinator import (
    TOPIC_RESUBSCRIBE_AFTER,
    TOPIC_SUBSCRIPTION_TTL,
    CleanSettings,
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_locate_robot,
    can_pause_cleaning,
    can_return_home,
    can_resume_cleaning,
    can_start_cleaning,
    can_start_dock_task,
    can_stop_cleaning,
    can_stop_dock_task,
    is_live_clean_setting_available,
)  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    FanLevel,
    MopHumidity,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkingStatus,
)

UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


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
    assert can_resume_cleaning(state)
    assert can_stop_cleaning(state)
    assert is_live_clean_setting_available(state)
    assert not can_edit_pending_clean_settings(state)
    assert not can_start_cleaning(state)
    assert not can_start_dock_task(state)


def test_return_home_available_when_idle_off_dock() -> None:
    """An idle robot away from the dock should expose return-to-base."""
    state = MagicMock()
    state.working_status = WorkingStatus.STANDBY
    state.is_docked = False
    state.is_returning = False
    state.is_station_active = False
    state.is_charging_to_resume = False

    assert can_return_home(state)

    state.is_docked = True
    assert not can_return_home(state)


def test_task_completed_blocks_return_home_until_docked() -> None:
    """Do not issue a second return command while TASK_COMPLETED is returning."""
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)

    assert state.is_returning
    assert not can_return_home(state)


def test_parsed_fault_blocks_robot_and_dock_commands() -> None:
    """Base-status faults should gate the same commands as explicit ERROR state."""
    idle = NarwalState(working_status=WorkingStatus.DOCKED)
    assert can_edit_pending_clean_settings(idle)
    assert can_start_cleaning(idle)
    assert can_locate_robot(idle)
    assert can_start_dock_task(idle)
    idle.has_error = True
    assert not can_edit_pending_clean_settings(idle)
    assert not can_start_cleaning(idle)
    assert not can_locate_robot(idle)
    assert not can_start_dock_task(idle)

    active = NarwalState(working_status=WorkingStatus.CLEANING)
    assert can_pause_cleaning(active)
    assert can_stop_cleaning(active)
    assert is_live_clean_setting_available(active)
    active.has_error = True
    assert not can_pause_cleaning(active)
    assert not can_stop_cleaning(active)
    assert not is_live_clean_setting_available(active)

    paused = NarwalState(working_status=WorkingStatus.CLEANING)
    paused.is_paused = True
    assert can_resume_cleaning(paused)
    paused.has_error = True
    assert not can_resume_cleaning(paused)

    dock = NarwalState(working_status=WorkingStatus.DOCKED)
    dock.station_activity = 1
    assert can_stop_dock_task(dock)
    dock.has_error = True
    assert not can_stop_dock_task(dock)


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
        coordinator._consecutive_failures = 0
        coordinator._max_failures = 5
        coordinator._consumable_poll_countdown = 99  # don't fire consumable poll in unit tests
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator.active_room_ids = None
        coordinator._active_room_plan_pending_until = 0.0
        coordinator._remapping_map_key = None
        coordinator._remapping_map_refresh_pending = False
        coordinator._remapping_map_refresh_attempts = 0
        coordinator._remapping_map_next_refresh = 0.0
        coordinator._last_display_map_resub = 0.0
        # Fresh subscription so renewal does not fire in unrelated tests.
        coordinator._last_topic_subscribe = time.monotonic()
        coordinator._last_task_details_refresh = time.monotonic()
        coordinator._prev_working_status = MagicMock()
        coordinator.active_room_ids = None
        coordinator._active_room_plan_pending_until = 0.0
        coordinator.update_interval = None
        # Prevent background task warnings
        mock_entry.async_create_background_task = MagicMock()
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
        coordinator.client.get_status = AsyncMock()

        result = await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 0
        assert result is coordinator.client.state

    async def test_task_status_refresh_runs_when_progress_endpoint_fails(self) -> None:
        """Unsupported progress details should not block robot task status refresh."""
        coordinator = self._make_coordinator()
        coordinator.client.get_clean_progress_info = AsyncMock(
            side_effect=Exception("unsupported")
        )
        coordinator.client.get_robot_task_status = AsyncMock()
        coordinator.async_set_updated_data = MagicMock()

        await coordinator._refresh_task_details(cleaning=True)

        coordinator.client.get_clean_progress_info.assert_awaited_once()
        coordinator.client.get_robot_task_status.assert_awaited_once()
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )

    async def test_poll_preserves_recent_active_working_status(self) -> None:
        """Poll only refreshes hardware fields while task metrics are fresh."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state.update_from_working_status({"3": 120})
        coordinator.client.get_status = AsyncMock()

        result = await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        assert result is coordinator.client.state

    async def test_push_update_resets_failure_counter(self) -> None:
        """_on_state_update resets _consecutive_failures to 0."""
        coordinator = self._make_coordinator()
        coordinator._consecutive_failures = 3

        # Mock methods called by _on_state_update
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = MagicMock()

        state = NarwalState()
        coordinator._on_state_update(state)

        assert coordinator._consecutive_failures == 0

    async def test_remapping_display_map_dropout_resubscribes(self) -> None:
        """Remapping also needs display_map recovery when broadcasts stall."""
        coordinator = self._make_coordinator()
        coordinator.client.last_display_map_age = 31.0
        coordinator._last_display_map_resub = time.monotonic() - 46.0
        coordinator.async_set_updated_data = MagicMock()
        scheduled_tasks = []

        def record_background_task(hass, coro, name):
            coro.close()
            scheduled_tasks.append(name)

        coordinator.config_entry.async_create_background_task.side_effect = (
            record_background_task
        )

        state = NarwalState(working_status=WorkingStatus.REMAPPING)
        state.map_data = MagicMock()
        coordinator._on_state_update(state)

        assert scheduled_tasks == ["narwal_resub"]

    def test_sync_active_rooms_preserves_station_phase_during_clean(self) -> None:
        """Station work during an active clean does not erase the requested rooms."""
        coordinator = self._make_coordinator()
        coordinator.active_room_ids = [1, 2]
        state = NarwalState()
        state.working_status = WorkingStatus.CLEANING
        state.last_active_working_status_time = time.monotonic()
        state.station_activity = 3

        coordinator._sync_active_room_ids(state)

        assert coordinator.active_room_ids == [1, 2]

    def test_sync_active_rooms_clears_after_clean_ends_with_station_phase(self) -> None:
        """Dock-only work after the clean has ended should clear stale room context."""
        coordinator = self._make_coordinator()
        coordinator.active_room_ids = [1, 2]
        coordinator._active_room_plan_pending_until = 0.0
        state = NarwalState()
        state.working_status = WorkingStatus.CHARGED
        state.station_activity = 4

        coordinator._sync_active_room_ids(state)

        assert coordinator.active_room_ids is None

    def test_sync_active_rooms_preserves_pending_start(self) -> None:
        """A room plan is retained briefly while a start command is taking effect."""
        coordinator = self._make_coordinator()
        coordinator.set_active_room_ids([1, 2])
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED

        coordinator._sync_active_room_ids(state)

        assert coordinator.active_room_ids == [1, 2]

    def test_sync_active_rooms_clears_expired_pending_start(self) -> None:
        """Pending room plans still clear if no active clean appears."""
        coordinator = self._make_coordinator()
        coordinator.set_active_room_ids([1, 2])
        coordinator._active_room_plan_pending_until = time.monotonic() - 1
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED

        coordinator._sync_active_room_ids(state)

        assert coordinator.active_room_ids is None

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
        c.client.get_status = AsyncMock()
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
        c.active_room_ids = None
        c._active_room_plan_pending_until = 0.0
        c._remapping_map_key = None
        c._remapping_map_refresh_pending = False
        c._remapping_map_refresh_attempts = 0
        c._remapping_map_next_refresh = 0.0
        c._last_display_map_resub = 0.0
        c._last_topic_subscribe = last_subscribe
        c._last_task_details_refresh = time.monotonic()
        c._prev_working_status = MagicMock()
        c.active_room_ids = None
        c._active_room_plan_pending_until = 0.0
        c.update_interval = None
        c.async_set_updated_data = MagicMock()
        return c

    @pytest.mark.asyncio
    async def test_renews_when_subscription_is_stale(self) -> None:
        """A poll past the renewal window re-sends the subscription."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        await c._async_update_data()
        c.client.subscribe_to_topics.assert_awaited_once()

    def test_remapping_completion_refreshes_dock_status(self) -> None:
        """Dock fields can be stale when remapping ends on the dock."""
        c = self._coordinator(time.monotonic())
        c._prev_working_status = WorkingStatus.REMAPPING
        c.client.state.working_status = WorkingStatus.DOCKED
        task = object()
        c._refresh_dock_status = MagicMock(return_value=task)

        c._on_state_update(c.client.state)

        c._refresh_dock_status.assert_called_once_with()
        c.hass.async_create_task.assert_called_once_with(task)

    @pytest.mark.asyncio
    async def test_skipped_renewal_keeps_retry_window_open(self) -> None:
        """A busy command channel should not mark a subscription as renewed."""
        last_subscribe = time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30)
        c = self._coordinator(last_subscribe)
        c.client.subscribe_to_topics = AsyncMock(return_value=False)

        await c._async_update_data()

        c.client.subscribe_to_topics.assert_awaited_once()
        assert c._last_topic_subscribe == last_subscribe

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
