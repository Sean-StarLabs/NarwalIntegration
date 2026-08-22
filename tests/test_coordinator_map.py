"""Tests for NarwalCoordinator map refresh logic.

Covers MAP-04 (post-cleaning map refresh) validation gaps:
  - _on_state_update triggers _fetch_missing_map when map_data is None
  - _was_cleaning / _prev_working_status tracks state transitions
  - Return-to-dock transition triggers dock status refresh
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.coordinator import (  # noqa: E402
    NarwalCoordinator,
    _map_refresh_key,
)
from custom_components.narwal.narwal_client import MapData, NarwalState  # noqa: E402
from custom_components.narwal.narwal_client.const import WorkingStatus  # noqa: E402


class TestCoordinatorMapRefresh:
    """Tests for coordinator map fetch and state transition detection."""

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
        coordinator.hass = mock_hass
        coordinator.config_entry = mock_entry
        coordinator.client = MagicMock()
        coordinator.client.state = NarwalState()
        coordinator._consecutive_failures = 0
        coordinator._max_failures = 5
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator._remapping_map_key = None
        coordinator._remapping_map_refresh_pending = False
        coordinator._remapping_map_refresh_attempts = 0
        coordinator._remapping_map_next_refresh = 0.0
        coordinator._last_display_map_resub = 0.0
        coordinator._last_status_resub = 0.0
        coordinator._last_task_details_refresh = time.monotonic()
        coordinator._prev_working_status = WorkingStatus.UNKNOWN
        coordinator.update_interval = None
        coordinator.async_set_updated_data = MagicMock()
        def _close_background_task(*args: object) -> None:
            for arg in args:
                if hasattr(arg, "close"):
                    arg.close()

        mock_entry.async_create_background_task = MagicMock(side_effect=_close_background_task)
        mock_hass.async_create_task = MagicMock(side_effect=_close_background_task)
        # Prevent TypeError on display_map dropout check when is_cleaning
        coordinator.client.last_display_map_age = 0.0
        return coordinator

    def test_missing_map_triggers_fetch(self) -> None:
        """When map_data is None and not already pending, schedule map fetch."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = None  # no map
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is True
        coordinator.config_entry.async_create_background_task.assert_called_once()
        # Verify the task name contains "map_fetch"
        call_args = coordinator.config_entry.async_create_background_task.call_args
        assert "map_fetch" in call_args[0][2]

    def test_map_present_no_fetch(self) -> None:
        """When map_data exists, no map fetch is triggered."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = MagicMock()  # map exists
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # No background task should be created for map fetch
        # (there might be other tasks, so check none have "map_fetch" in name)
        for call in coordinator.config_entry.async_create_background_task.call_args_list:
            assert "map_fetch" not in call[0][2]

    def test_map_fetch_not_duplicated(self) -> None:
        """When map fetch is already pending, don't schedule another."""
        coordinator = self._make_coordinator()
        coordinator._map_fetch_pending = True
        state = NarwalState()
        state.map_data = None
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # No new background task created
        coordinator.config_entry.async_create_background_task.assert_not_called()

    def test_remapping_with_existing_map_arms_post_remap_refresh(self) -> None:
        """A remap tracks the old map without consuming retries while active."""
        coordinator = self._make_coordinator()
        state = NarwalState(working_status=WorkingStatus.REMAPPING)
        state.map_data = MapData(map_id=1, width=2, height=2, compressed_map=b"old")

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is False
        assert coordinator._remapping_map_key == _map_refresh_key(state.map_data)
        assert coordinator._remapping_map_refresh_pending is True
        assert coordinator._remapping_map_refresh_attempts == 0
        coordinator.config_entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_map_fetch_during_remapping_arms_post_remap_refresh(
        self,
    ) -> None:
        """A mid-remap startup without a map still refreshes after remap exit."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        state = NarwalState(working_status=WorkingStatus.REMAPPING)
        state.map_data = None

        coordinator._on_state_update(state)

        assert coordinator._remapping_map_refresh_pending is True
        assert coordinator._remapping_map_key is None
        assert coordinator._map_fetch_pending is True

        async def get_map() -> MapData:
            coordinator.client.state = NarwalState(working_status=WorkingStatus.REMAPPING)
            coordinator.client.state.map_data = old_map
            return old_map

        coordinator.client.get_map = AsyncMock(side_effect=get_map)
        coordinator.client.supports_broadcasts = False

        await coordinator._fetch_static_map(reason="missing")

        assert coordinator._remapping_map_refresh_pending is True
        assert coordinator._remapping_map_key == _map_refresh_key(old_map)

        coordinator.config_entry.async_create_background_task.reset_mock()
        coordinator._prev_working_status = WorkingStatus.REMAPPING
        exit_state = NarwalState(working_status=WorkingStatus.STANDBY)
        exit_state.map_data = old_map

        coordinator._on_state_update(exit_state)

        assert coordinator._map_fetch_pending is True
        coordinator.config_entry.async_create_background_task.assert_called_once()

    def test_remapping_exit_triggers_static_map_refresh(self) -> None:
        """Static map refresh starts once remapping has finished."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        coordinator._prev_working_status = WorkingStatus.REMAPPING
        coordinator._remapping_map_key = _map_refresh_key(old_map)
        coordinator._remapping_map_refresh_pending = True
        coordinator._remapping_map_refresh_attempts = 2
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.map_data = old_map

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is True
        assert coordinator._remapping_map_refresh_attempts == 0
        assert coordinator._remapping_map_next_refresh == 0.0
        coordinator.config_entry.async_create_background_task.assert_called_once()
        assert (
            "map_fetch"
            in coordinator.config_entry.async_create_background_task.call_args[0][2]
        )

    def test_remapping_retry_continues_after_status_leaves_remapping(self) -> None:
        """A delayed replacement map must not require another REMAPPING update."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        previous_key = _map_refresh_key(old_map)
        coordinator._prev_working_status = WorkingStatus.STANDBY
        coordinator._remapping_map_key = previous_key
        coordinator._remapping_map_refresh_pending = True
        coordinator._remapping_map_refresh_attempts = 1
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.map_data = old_map

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is True
        coordinator.config_entry.async_create_background_task.assert_called_once()

    def test_remapping_retry_waits_for_refresh_delay(self) -> None:
        """Unchanged map retries are paced instead of refetched every broadcast."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        previous_key = _map_refresh_key(old_map)
        coordinator._prev_working_status = WorkingStatus.STANDBY
        coordinator._remapping_map_key = previous_key
        coordinator._remapping_map_refresh_pending = True
        coordinator._remapping_map_refresh_attempts = 1
        coordinator._remapping_map_next_refresh = time.monotonic() + 60.0
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.map_data = old_map

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is False
        coordinator.config_entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_remapping_static_map_refresh_retries_unchanged_map(self) -> None:
        """An unchanged remap fetch remains eligible for a later retry."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        previous_key = _map_refresh_key(old_map)
        coordinator.client.state.map_data = old_map
        coordinator.client.get_map = AsyncMock(return_value=old_map)
        coordinator.client.supports_broadcasts = False
        coordinator._map_fetch_pending = True
        coordinator._remapping_map_key = previous_key
        coordinator._remapping_map_refresh_pending = True

        await coordinator._fetch_static_map(
            reason="remapping",
            previous_key=previous_key,
        )

        assert coordinator._map_fetch_pending is False
        assert coordinator._remapping_map_refresh_pending is True
        assert coordinator._remapping_map_key == previous_key
        assert coordinator._remapping_map_refresh_attempts == 1
        assert coordinator._remapping_map_next_refresh > time.monotonic()

    @pytest.mark.asyncio
    async def test_remapping_static_map_refresh_clears_when_map_changes(self) -> None:
        """A changed static map ends the remap refresh retry loop."""
        coordinator = self._make_coordinator()
        old_map = MapData(map_id=1, width=2, height=2, compressed_map=b"old")
        new_map = MapData(map_id=1, width=2, height=2, compressed_map=b"new")
        previous_key = _map_refresh_key(old_map)
        coordinator.client.state.map_data = old_map

        async def get_map() -> MapData:
            coordinator.client.state.map_data = new_map
            return new_map

        coordinator.client.get_map = AsyncMock(side_effect=get_map)
        coordinator.client.supports_broadcasts = False
        coordinator._map_fetch_pending = True
        coordinator._remapping_map_key = previous_key
        coordinator._remapping_map_refresh_pending = True
        coordinator._remapping_map_refresh_attempts = 1

        await coordinator._fetch_static_map(
            reason="remapping",
            previous_key=previous_key,
        )

        assert coordinator._map_fetch_pending is False
        assert coordinator._remapping_map_refresh_pending is False
        assert coordinator._remapping_map_key is None
        assert coordinator._remapping_map_refresh_attempts == 0
        assert coordinator._remapping_map_next_refresh == 0.0
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )

    def test_cleaning_to_standby_triggers_dock_refresh(self) -> None:
        """Transition from CLEANING to STANDBY triggers dock status refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.CLEANING
        state = NarwalState()
        state.map_data = MagicMock()  # avoid map fetch
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # hass.async_create_task should be called for dock refresh
        coordinator.hass.async_create_task.assert_called_once()
        assert coordinator._prev_working_status == WorkingStatus.STANDBY

    def test_task_completed_to_standby_triggers_dock_refresh(self) -> None:
        """Transition from TASK_COMPLETED to docked state refreshes stale dock fields."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.TASK_COMPLETED
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_called_once()

    def test_task_completed_to_charged_triggers_dock_refresh(self) -> None:
        """Direct transition to CHARGED also refreshes stale dock fields."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.TASK_COMPLETED
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.CHARGED

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_called_once()

    def test_cleaning_alt_to_standby_triggers_dock_refresh(self) -> None:
        """Transition from CLEANING_ALT to STANDBY also triggers dock refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.CLEANING_ALT
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_called_once()

    def test_standby_to_cleaning_no_dock_refresh(self) -> None:
        """Transition from STANDBY to CLEANING does NOT trigger dock refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.STANDBY
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.CLEANING

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_not_called()

    def test_prev_working_status_tracks_transitions(self) -> None:
        """_prev_working_status updates after each _on_state_update call."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = MagicMock()

        # UNKNOWN -> CLEANING
        state.working_status = WorkingStatus.CLEANING
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.CLEANING

        # CLEANING -> STANDBY
        state.working_status = WorkingStatus.STANDBY
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.STANDBY

        # STANDBY -> DOCKED
        state.working_status = WorkingStatus.DOCKED
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.DOCKED

    def test_push_update_resets_fast_poll(self) -> None:
        """Push update during fast polling restores normal polling."""
        coordinator = self._make_coordinator()
        coordinator._fast_poll_remaining = 3

        from custom_components.narwal.coordinator import POLL_INTERVAL

        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        assert coordinator._fast_poll_remaining == 0
        assert coordinator.update_interval == POLL_INTERVAL
