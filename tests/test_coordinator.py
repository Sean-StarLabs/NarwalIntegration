"""Tests for NarwalCoordinator resilience -- failure buffering and push reset.

Verifies the coordinator returns stale data on transient failures, raises
UpdateFailed after the threshold, and resets counters on success/push.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    NarwalConnectionError,
    NarwalState,
    WorkingStatus,
)
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402


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
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator._last_display_map_resub = 0.0
        coordinator._last_maintenance_refresh = 0.0
        coordinator._prev_working_status = MagicMock()
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

    async def test_stale_data_after_max_failures(self) -> None:
        """_async_update_data raises after the failure threshold."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        for _ in range(4):
            result = await coordinator._async_update_data()
            assert result is coordinator.client.state

        with pytest.raises(UpdateFailed):
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

    async def test_poll_preserves_recent_active_task_marker(self) -> None:
        """A poll only refreshes battery while task status is fresher."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state.refresh_active_cleaning()
        coordinator.client.get_status = AsyncMock()

        await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)

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

    async def test_maintenance_details_refresh_when_forced(self) -> None:
        """Idle maintenance counters can be populated independently of a task."""
        coordinator = self._make_coordinator()
        coordinator.config_entry.data["product_key"] = "QxMSPG6VSO"
        coordinator.client.get_clean_progress_info = AsyncMock()

        await coordinator._refresh_maintenance_details(force=True)

        coordinator.client.get_clean_progress_info.assert_awaited_once_with()

    async def test_maintenance_details_refresh_is_throttled(self) -> None:
        """Normal polls do not query maintenance details every minute."""
        coordinator = self._make_coordinator()
        coordinator.config_entry.data["product_key"] = "QxMSPG6VSO"
        coordinator.client.get_clean_progress_info = AsyncMock()

        await coordinator._refresh_maintenance_details(force=True)
        await coordinator._refresh_maintenance_details()

        coordinator.client.get_clean_progress_info.assert_awaited_once_with()

    async def test_maintenance_details_retry_after_failure(self) -> None:
        """A failed maintenance refresh does not suppress the next attempt."""
        coordinator = self._make_coordinator()
        coordinator.config_entry.data["product_key"] = "QxMSPG6VSO"
        coordinator.client.get_clean_progress_info = AsyncMock(
            side_effect=[NarwalConnectionError("recv timeout"), None]
        )

        await coordinator._refresh_maintenance_details()
        await coordinator._refresh_maintenance_details()

        assert coordinator.client.get_clean_progress_info.await_count == 2

    async def test_maintenance_details_skip_unsupported_models(self) -> None:
        """Models without maintenance entities are not polled for their data."""
        coordinator = self._make_coordinator()
        coordinator.client.get_clean_progress_info = AsyncMock()

        await coordinator._refresh_maintenance_details(force=True)

        coordinator.client.get_clean_progress_info.assert_not_awaited()

    async def test_status_recovery_preserves_stale_active_state(self) -> None:
        """Recovery avoids a full status update without fresh active telemetry."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.subscribe_to_topics = AsyncMock()
        coordinator.client.get_status = AsyncMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock()

        await coordinator._recover_status_broadcasts()

        coordinator.client.get_status.assert_not_awaited()
        coordinator.client.get_clean_progress_info.assert_awaited_once_with()
        coordinator.client.get_robot_task_status.assert_awaited_once_with()

    async def test_status_recovery_avoids_fresh_base_status_poll(self) -> None:
        """Recovery never overlays an active clean with stale dock status."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.state.mark_active_cleaning()
        coordinator.client.subscribe_to_topics = AsyncMock()
        coordinator.client.get_status = AsyncMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock()

        await coordinator._recover_status_broadcasts()

        coordinator.client.get_status.assert_not_awaited()
        coordinator.client.get_clean_progress_info.assert_awaited_once_with()
        coordinator.client.get_robot_task_status.assert_awaited_once_with()

    async def test_status_recovery_renews_active_task_marker(self) -> None:
        """Current task progress keeps stale base status from ending a clean."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.subscribe_to_topics = AsyncMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock(
            return_value=SimpleNamespace(data={"1": 1, "2": {"1": 42}})
        )

        await coordinator._recover_status_broadcasts()

        assert coordinator.client.state.has_recent_active_working_status
        assert (
            coordinator.client._last_active_working_status_time
            == coordinator.client.state.last_active_working_status_time
        )

    async def test_status_recovery_rejects_idle_zero_progress(self) -> None:
        """An idle zero-progress response cannot revive stale cleaning state."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.state.working_status = WorkingStatus.CLEANING
        coordinator.client.subscribe_to_topics = AsyncMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock(
            return_value=SimpleNamespace(data={"1": 1, "2": {"1": 0}})
        )

        await coordinator._recover_status_broadcasts()

        assert not coordinator.client.state.has_recent_active_working_status

    async def test_status_recovery_preserves_accepted_zero_progress(self) -> None:
        """An accepted task remains active while it still reports zero progress."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.state.mark_active_cleaning()
        coordinator.client.state.last_active_working_status_time = 0.0
        coordinator.client._last_active_working_status_time = 0.0
        coordinator.client.subscribe_to_topics = AsyncMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock(
            return_value=SimpleNamespace(data={"1": 1, "2": {"1": 0}})
        )

        await coordinator._recover_status_broadcasts()

        assert coordinator.client.state.has_recent_active_working_status

    async def test_task_detail_refresh_renews_client_active_marker(self) -> None:
        """Task-detail polling also renews stale-base suppression."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock(
            return_value=SimpleNamespace(data={"1": 1, "2": {"1": 42}})
        )

        await coordinator._refresh_task_details(cleaning=True)

        assert (
            coordinator.client._last_active_working_status_time
            == coordinator.client.state.last_active_working_status_time
        )

    async def test_completed_task_details_do_not_renew_active_marker(self) -> None:
        """A completed task response does not revive cleaning state."""
        coordinator = self._make_coordinator()
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.get_clean_progress_info = AsyncMock()
        coordinator.client.get_robot_task_status = AsyncMock(
            return_value=SimpleNamespace(data={"1": 1, "2": {"1": 100}})
        )

        await coordinator._refresh_task_details(cleaning=True)

        assert not coordinator.client.state.has_recent_active_working_status
