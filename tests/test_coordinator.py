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
    can_pause_cleaning,
    can_prepare_clean_start,
    can_start_cleaning,
    is_clean_session_context,
    is_live_clean_setting_available,
    is_narwal_task_busy,
)  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MopHumidity,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkingStatus,
    WorkMode,
)
from custom_components.narwal.narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
)

UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


def test_pause_available_with_stale_unconfirmed_return_flag() -> None:
    """A stale field 3.7 alone must not hide Pause during an active clean."""
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 4, "7": 1}})
    state.last_active_working_status_time = 0.0

    assert can_pause_cleaning(state)


class _RoomSelectionStore:
    """Minimal storage double for room-selection persistence tests."""

    def __init__(self) -> None:
        self.data: object | None = None

    async def async_load(self) -> object | None:
        """Return stored data."""
        return self.data

    async def async_save(self, data: object) -> None:
        """Store data."""
        self.data = data


def _docked_state() -> NarwalState:
    """Return an idle on-dock state."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    return state


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


def test_selected_clean_rooms_fall_back_to_all_rooms() -> None:
    """No room selection means the native start command cleans every room."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {}

    assert coordinator.selected_clean_room_ids_for([4, 5]) == [4, 5]


def test_selected_clean_rooms_prune_stale_ids_and_are_map_scoped() -> None:
    """Known selected rooms remain scoped without mutating stale selections."""
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
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []


def test_selected_clean_rooms_fall_back_when_every_selected_room_vanished() -> None:
    """A vanished explicit selection continues to fail closed."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {"upstairs": {99}}

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []
    assert coordinator.has_selected_clean_rooms(map_id="upstairs")


def test_unidentified_map_selection_remains_explicit_after_identification() -> None:
    """Learning a map id cannot broaden an unresolved explicit selection."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator._room_selection_store_loaded = True

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4]
    assert coordinator.has_selected_clean_rooms(map_id="100")
    assert coordinator.is_room_selected_for_clean(4, map_id="100")

    coordinator._schedule_room_selection_save = MagicMock()
    coordinator.set_room_selected_for_clean(4, False, map_id="100")
    coordinator.set_room_selected_for_clean(5, True, map_id="100")

    assert None not in coordinator.selected_clean_rooms
    assert coordinator.selected_clean_rooms == {"100": {5}}


async def test_room_selection_store_preserves_disappeared_selected_room() -> None:
    """Restart cannot broaden a stale explicit selection to every current room."""
    store = _RoomSelectionStore()
    before = NarwalCoordinator.__new__(NarwalCoordinator)
    before.selected_clean_rooms = {"upstairs": {4}}
    before._room_selection_store = store
    before._room_selection_save_lock = asyncio.Lock()
    before._room_selection_store_loaded = True

    await before._async_save_room_selections()

    after = NarwalCoordinator.__new__(NarwalCoordinator)
    after.selected_clean_rooms = {}
    after._room_selection_store = store
    after._room_selection_store_loaded = False
    await after._async_restore_room_selections()

    assert after.selected_clean_room_ids_for([5], map_id="upstairs") == []
    assert after.is_room_selected_for_clean(4, map_id="upstairs")


async def test_room_store_restores_customized_profile_without_entities() -> None:
    """Disabled room controls cannot lose their raw customized values."""
    store = _RoomSelectionStore()
    before = NarwalCoordinator.__new__(NarwalCoordinator)
    before.selected_clean_rooms = {}
    before.room_clean_settings = {
        ("upstairs", 4): RoomCleanSettings(
            work_mode=WorkMode.MOP,
            fan=FanLevel.STRONG,
            passes=3,
        )
    }
    before.room_clean_settings_customized = {
        ("upstairs", 4): {"work_mode", "fan", "passes"}
    }
    before._room_selection_store = store
    before._room_selection_save_lock = asyncio.Lock()
    before._room_selection_store_loaded = True

    await before._async_save_room_selections()

    after = NarwalCoordinator.__new__(NarwalCoordinator)
    after.selected_clean_rooms = {}
    after.room_clean_settings = {}
    after.room_clean_settings_customized = {}
    after._room_selection_store = store
    after._room_selection_store_loaded = False
    await after._async_restore_room_selections()

    restored = after.room_clean_settings[("upstairs", 4)]
    assert restored.work_mode == WorkMode.MOP
    assert restored.fan == FanLevel.STRONG
    assert restored.passes == 3
    assert after.room_clean_settings_customized == {
        ("upstairs", 4): {"work_mode", "fan", "passes"}
    }


async def test_room_selection_load_failure_cannot_overwrite_stored_state() -> None:
    """A failed restore must not replace unread selections during shutdown."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(side_effect=OSError)
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == []
    coordinator._room_selection_store.async_save.assert_not_awaited()


async def test_selection_change_cannot_authorize_unread_profile_overwrite() -> None:
    """A room toggle after failed restore cannot replace durable profiles."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {
        ("upstairs", 4): RoomCleanSettings(fan=FanLevel.MUTE)
    }
    coordinator.room_clean_settings_customized = {("upstairs", 4): {"fan"}}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[OSError, {"maps": [], "profiles": [{"durable": "profile"}]}]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(4, True, map_id="upstairs")
    await coordinator._async_save_room_selections()

    assert coordinator._room_selection_store_loaded
    assert not coordinator._room_profile_store_loaded
    coordinator._room_selection_store.async_save.assert_awaited_once_with(
        {
            "maps": [{"map_id": "upstairs", "room_ids": [4]}],
            "profiles": [{"durable": "profile"}],
        }
    )


async def test_selection_retry_preserves_other_stored_maps() -> None:
    """A local toggle after a failed read must merge unrelated stored maps."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.data = NarwalState()
    coordinator.client = MagicMock()
    coordinator.client.state = coordinator.data
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[
            OSError,
            {
                "maps": [
                    {"map_id": "100", "room_ids": [4]},
                    {"map_id": "200", "room_ids": [7]},
                ],
                "profiles": [],
            },
        ]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(5, True, map_id="100")
    await coordinator._async_save_room_selections()

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == [
        {"map_id": "100", "room_ids": [5]},
        {"map_id": "200", "room_ids": [7]},
    ]


def test_map_identification_migrates_unresolved_profiles_with_selection() -> None:
    """Resolving a map keeps its customized room profiles attached."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    profile = RoomCleanSettings(work_mode=WorkMode.VACUUM)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator.room_clean_settings = {(None, 4): profile}
    coordinator.room_clean_settings_customized = {(None, 4): {"work_mode"}}
    coordinator._room_selection_store_loaded = True
    coordinator._schedule_room_selection_save = MagicMock()

    coordinator.set_room_selected_for_clean(5, True, map_id="100")

    assert coordinator.selected_clean_rooms == {"100": {4, 5}}
    assert coordinator.room_clean_settings == {("100", 4): profile}
    assert coordinator.room_clean_settings_customized == {
        ("100", 4): {"work_mode"}
    }


def test_profile_resolution_does_not_require_another_selection_toggle() -> None:
    """A fetched map immediately attaches unresolved profiles to native starts."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.clean_settings = CleanSettings(work_mode=WorkMode.VACUUM_AND_MOP)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator.room_clean_settings = {
        (None, 4): RoomCleanSettings(work_mode=WorkMode.VACUUM)
    }
    coordinator.room_clean_settings_customized = {(None, 4): {"work_mode"}}
    coordinator._room_selection_store_loaded = True
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert coordinator.selected_clean_rooms == {"100": {4}}
    assert set(coordinator.room_clean_settings) == {("100", 4)}
    assert coordinator.room_clean_settings[("100", 4)].work_mode == WorkMode.VACUUM


async def test_newer_unresolved_selection_supersedes_same_stored_map() -> None:
    """A post-failure unresolved choice wins when its map becomes known."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.data = NarwalState()
    coordinator.client = MagicMock()
    coordinator.client.state = coordinator.data
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[
            OSError,
            {
                "maps": [{"map_id": "100", "room_ids": [4]}],
                "profiles": [],
            },
        ]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(5, True)
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [5]
    assert coordinator.selected_clean_rooms == {"100": {5}}

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == [
        {
            "map_id": None,
            "room_ids": [5],
            "pending_map_resolution": True,
        },
        {"map_id": "100", "room_ids": [4]},
    ]
    restarted = NarwalCoordinator.__new__(NarwalCoordinator)
    restarted.selected_clean_rooms = {}
    restarted.room_clean_settings = {}
    restarted.room_clean_settings_customized = {}
    restarted._room_selection_store = _RoomSelectionStore()
    restarted._room_selection_store.data = saved
    restarted._room_selection_store_loaded = False
    restarted._room_profile_store_loaded = False
    restarted._room_selection_dirty_maps = set()
    restarted._schedule_room_selection_save = MagicMock()

    await restarted._async_restore_room_selections()

    assert restarted.selected_clean_room_ids_for([4, 5], map_id="100") == [5]
    assert restarted.selected_clean_rooms == {"100": {5}}


async def test_persisted_unresolved_precedence_survives_failed_read_retry() -> None:
    """A shutdown retry cannot treat persisted precedence as a local deletion."""
    stored = {
        "maps": [
            {
                "map_id": None,
                "room_ids": [5],
                "pending_map_resolution": True,
            },
            {"map_id": "100", "room_ids": [4]},
        ],
        "profiles": [],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[OSError, stored]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == stored["maps"]

    restarted = NarwalCoordinator.__new__(NarwalCoordinator)
    restarted.selected_clean_rooms = {}
    restarted.room_clean_settings = {}
    restarted.room_clean_settings_customized = {}
    restarted._room_selection_store = _RoomSelectionStore()
    restarted._room_selection_store.data = saved
    restarted._room_selection_store_loaded = False
    restarted._room_profile_store_loaded = False
    restarted._room_selection_dirty_maps = set()
    restarted._schedule_room_selection_save = MagicMock()
    await restarted._async_restore_room_selections()

    assert restarted.selected_clean_room_ids_for([4, 5], map_id="100") == [5]


async def test_newer_unresolved_profile_overrides_scoped_profile_after_restart() -> None:
    """Profile resolution preserves a newer edit made before map identification."""
    store = _RoomSelectionStore()
    store.data = {
        "maps": [{"map_id": "100", "room_ids": [4]}],
        "profiles": [
            {
                "map_id": "100",
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM_AND_MOP)},
            },
            {
                "map_id": None,
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM)},
                "pending_map_resolution": True,
            },
        ],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.clean_settings = CleanSettings()
    coordinator._room_selection_store = store
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert set(coordinator.room_clean_settings) == {("100", 4)}


async def test_unresolved_profile_edit_preserves_other_scoped_fields() -> None:
    """Resolution merges newer fields without replacing scoped customization."""
    store = _RoomSelectionStore()
    store.data = {
        "maps": [{"map_id": "100", "room_ids": [4]}],
        "profiles": [
            {
                "map_id": "100",
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM)},
            },
            {
                "map_id": None,
                "room_id": 4,
                "values": {"fan": int(FanLevel.STRONG)},
                "pending_map_resolution": True,
            },
        ],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.clean_settings = CleanSettings(work_mode=WorkMode.VACUUM_AND_MOP)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = store
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert settings[4].fan == FanLevel.STRONG
    assert coordinator.room_clean_settings_customized == {
        ("100", 4): {"work_mode", "fan"}
    }


async def test_room_selection_write_failure_does_not_escape() -> None:
    """Store write failures are logged without aborting coordinator shutdown."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {"100": {4}}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_save = AsyncMock(
        side_effect=PermissionError
    )
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_selection_dirty_maps = {"100"}

    await coordinator._async_save_room_selections()

    assert coordinator._room_selection_dirty_maps == {"100"}


async def test_cancelled_room_save_cannot_overwrite_newer_selection() -> None:
    """Serialization remains held until a cancelled Store write completes."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {"upstairs": {4}}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_selection_save_lock = asyncio.Lock()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    saved: list[object] = []

    async def save(data: object) -> None:
        if not saved:
            first_started.set()
            await release_first.wait()
        saved.append(data)

    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_save = AsyncMock(side_effect=save)

    older = asyncio.create_task(coordinator._async_save_room_selections())
    await first_started.wait()
    coordinator.selected_clean_rooms["upstairs"].add(5)
    newer = asyncio.create_task(coordinator._async_save_room_selections())
    older.cancel()
    await asyncio.sleep(0)
    older.cancel()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(older, newer, return_exceptions=True)

    assert saved[-1]["maps"] == [
        {"map_id": "upstairs", "room_ids": [4, 5]}
    ]


async def test_malformed_room_selection_store_remains_non_authoritative() -> None:
    """Malformed nested data cannot enable starts or be overwritten."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        return_value={"maps": [{"map_id": "100", "room_ids": "4"}]}
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == []
    coordinator._room_selection_store.async_save.assert_not_awaited()


def test_selected_clean_room_presence_is_map_scoped() -> None:
    """Whole-floor setup can distinguish explicit selections per map."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {"upstairs": {5}}

    assert coordinator.has_selected_clean_rooms(map_id="upstairs")
    assert not coordinator.has_selected_clean_rooms(map_id="downstairs")


def test_active_clean_settings_follow_current_room_and_runtime_updates() -> None:
    """Live controls report dispatched room profiles instead of pending globals."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.current_room_id = 5
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}
    requested = {
        4: RoomCleanSettings(fan=FanLevel.NORMAL),
        5: RoomCleanSettings(fan=FanLevel.STRONG),
    }

    coordinator.record_accepted_clean_start(requested)

    assert coordinator.active_clean_setting("fan") == FanLevel.STRONG
    assert coordinator.active_room_clean_settings[5] is not requested[5]

    coordinator.set_active_clean_setting("fan", FanLevel.DEEP)

    assert coordinator.active_clean_setting("fan") == FanLevel.DEEP
    assert all(
        settings.fan == FanLevel.DEEP
        for settings in coordinator.active_room_clean_settings.values()
    )


def test_runtime_setting_is_retained_without_reconstructed_room_profiles() -> None:
    """An accepted live command remains visible without startup task profiles."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}
    coordinator.active_clean_setting_overrides = {}

    coordinator.set_active_clean_setting("fan", FanLevel.STRONG)

    assert coordinator.active_clean_setting("fan") == FanLevel.STRONG

    state.working_status = WorkingStatus.STANDBY
    coordinator._sync_active_clean_context(state)

    assert coordinator.active_clean_setting_overrides == {}


def test_mixed_active_clean_uses_current_room_mode_for_live_controls() -> None:
    """Runtime control applicability follows the room currently being cleaned."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.current_room_id = 5
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}

    coordinator.record_accepted_clean_start(
        {
            4: RoomCleanSettings(work_mode=WorkMode.MOP),
            5: RoomCleanSettings(work_mode=WorkMode.VACUUM),
        }
    )

    assert coordinator.active_clean_work_mode is None
    assert coordinator.clean_setting_applicability_mode(live=True) == WorkMode.VACUUM

    state.current_room_id = 4
    assert coordinator.clean_setting_applicability_mode(live=True) == WorkMode.MOP


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


def test_task_completed_remains_busy_until_terminal_dock_state() -> None:
    """TASK_COMPLETED is the return leg, not an editable idle state."""
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
    state.dock_presence = 6

    assert state.is_docked
    assert is_clean_session_context(state)
    assert is_narwal_task_busy(state)
    assert not can_edit_pending_clean_settings(state)
    assert not can_start_cleaning(state)


def test_remapping_does_not_expose_live_clean_settings() -> None:
    """Map-building tasks do not accept live clean-setting commands."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)

    assert not is_live_clean_setting_available(state)


@pytest.mark.parametrize(
    "working_status", (WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR)
)
def test_terminal_status_does_not_expose_live_clean_settings(
    working_status: WorkingStatus,
) -> None:
    """Accepted-start context cannot expose controls after a terminal status."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.assume_robot_clean()
    state.working_status = working_status

    assert not is_live_clean_setting_available(state)


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
        coordinator.active_clean_work_mode = None
        coordinator.active_room_clean_settings = {}
        coordinator.update_interval = None
        def _close_background_task(*args: object) -> None:
            for arg in args:
                if hasattr(arg, "close"):
                    arg.close()

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

    async def test_poll_retries_failed_room_store_restore(self) -> None:
        """Polling retries a transient Store read failure without user action."""
        coordinator = self._make_coordinator()
        coordinator._room_selection_store = MagicMock()
        coordinator._room_selection_store.async_load = AsyncMock(
            side_effect=[OSError, None]
        )
        coordinator._room_selection_save_lock = asyncio.Lock()
        coordinator._room_selection_store_loaded = False
        coordinator._room_profile_store_loaded = False
        type(coordinator.client).connected = PropertyMock(return_value=False)

        await coordinator._async_restore_room_selections()
        assert not coordinator._room_selection_store_loaded
        assert not coordinator._room_profile_store_loaded

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._room_selection_store_loaded
        assert coordinator._room_profile_store_loaded
        assert coordinator._room_selection_store.async_load.await_count == 2

    async def test_poll_restore_is_serialized_with_room_store_save(self) -> None:
        """A retry cannot apply an old read after a concurrent save."""
        coordinator = self._make_coordinator()
        coordinator.selected_clean_rooms = {}
        coordinator.room_clean_settings = {}
        coordinator.room_clean_settings_customized = {}
        coordinator._room_selection_dirty_maps = set()
        coordinator._room_profile_pending_resolution = set()
        coordinator._room_selection_save_lock = asyncio.Lock()
        coordinator._room_selection_store_loaded = False
        coordinator._room_profile_store_loaded = False
        coordinator._schedule_room_selection_save = MagicMock()
        type(coordinator.client).connected = PropertyMock(return_value=False)
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def delayed_load() -> object:
            read_started.set()
            await release_read.wait()
            return {
                "maps": [{"map_id": "upstairs", "room_ids": [4]}],
                "profiles": [],
            }

        coordinator._room_selection_store = MagicMock()
        coordinator._room_selection_store.async_load = AsyncMock(
            side_effect=delayed_load
        )
        coordinator._room_selection_store.async_save = AsyncMock()

        poll_task = asyncio.create_task(coordinator._async_update_data())
        await read_started.wait()
        coordinator.set_room_selected_for_clean(5, True, map_id="upstairs")
        save_task = asyncio.create_task(coordinator._async_save_room_selections())
        release_read.set()
        await poll_task
        await save_task

        assert coordinator.selected_clean_rooms == {"upstairs": {5}}
        coordinator._room_selection_store.async_save.assert_awaited_once()
        saved = coordinator._room_selection_store.async_save.await_args.args[0]
        assert saved["maps"] == [{"map_id": "upstairs", "room_ids": [5]}]

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
        coordinator.active_room_clean_settings = {4: RoomCleanSettings()}
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = WorkingStatus.CLEANING
        state = NarwalState()
        state.update_from_base_status({"3": {"1": int(WorkingStatus.DOCKED), "3": 6}})

        coordinator._on_state_update(state)

        assert coordinator.active_clean_work_mode is None
        assert coordinator.active_room_clean_settings == {}

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

    async def test_partial_status_poll_only_marks_dock_state_stale(self) -> None:
        """A battery-only response proves connectivity, but not dock freshness."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 0
        assert not coordinator.has_fresh_state

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

    async def test_prepare_clean_start_stops_single_safe_dock_blocker(self) -> None:
        """A clean-start intent clears one known safe dock blocker first."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()

        async def stop_task(task: str | None = None) -> CommandResponse:
            assert task == DOCK_TASK_EMPTY_DUSTBIN
            state.station_activity = 0
            return CommandResponse(result_code=CommandResult.SUCCESS)

        coordinator.client.stop_dock_task = AsyncMock(side_effect=stop_task)

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )
        assert coordinator.async_refresh_dock_status.await_count == 2
        assert can_start_cleaning(state)

    async def test_prepare_clean_start_rejects_failed_initial_refresh(self) -> None:
        """Preparation does not act when its initial state refresh fails."""
        coordinator = self._make_coordinator()
        coordinator.async_refresh_dock_status = AsyncMock(return_value=False)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not await coordinator.async_prepare_clean_start()

        coordinator.async_refresh_dock_status.assert_awaited_once()
        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_rejects_failed_dock_stop(self) -> None:
        """Preparation does not start after the dock rejects its required stop."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.CONFLICT)
        )

        assert not await coordinator.async_prepare_clean_start()

        coordinator.async_refresh_dock_status.assert_awaited_once()
        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )

    async def test_prepare_clean_start_rejects_failed_post_stop_refresh(self) -> None:
        """An accepted stop still requires authoritative refreshed state."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(
            side_effect=(True, False)
        )
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        assert not await coordinator.async_prepare_clean_start()

        assert coordinator.async_refresh_dock_status.await_count == 2
        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )

    @pytest.mark.parametrize(
        ("task", "fields"),
        [
            (DOCK_TASK_DRY_MOP, ("8", "9")),
            (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
            (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
        ],
    )
    async def test_prepare_clean_start_keeps_typed_drying_task(
        self,
        task: str,
        fields: tuple[str, str],
    ) -> None:
        """A new clean lets firmware hand off typed drying work."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.set_dock_drying_task(
            task,
            elapsed=60,
            target=180,
            fields=fields,
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock()

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()
        assert can_prepare_clean_start(state, allow_dock_stop=False)

        coordinator.client.stop_dock_task.assert_not_awaited()
        assert coordinator.async_refresh_dock_status.await_count == 1
        assert can_start_cleaning(state)

    async def test_prepare_clean_start_rejects_lingering_dock_task_after_stop(self) -> None:
        """Accepted stop is not enough if refreshed telemetry still shows a task."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        assert can_prepare_clean_start(state)
        assert not await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )
        assert coordinator.async_refresh_dock_status.await_count == 2

    async def test_prepare_clean_start_rejects_dock_stop_when_disabled(self) -> None:
        """No-stop preparation mode should never cancel dock maintenance."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 2
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not can_prepare_clean_start(state, allow_dock_stop=False)
        assert not await coordinator.async_prepare_clean_start(allow_dock_stop=False)

        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_accepts_wash_follow_on_drying(self) -> None:
        """Stopping a wash may hand off mop drying to the clean command."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 2
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()

        async def stop_task(task: str | None = None) -> CommandResponse:
            assert task == DOCK_TASK_WASH_MOP
            state.station_activity = 0
            state.set_dock_drying_task(
                DOCK_TASK_DRY_MOP,
                elapsed=0,
                target=18000,
                fields=("8", "9"),
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        coordinator.client.stop_dock_task = AsyncMock(side_effect=stop_task)

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_WASH_MOP
        )
        assert coordinator.async_refresh_dock_status.await_count == 2
        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)

    async def test_prepare_clean_start_allows_multiple_typed_dryers(self) -> None:
        """Multiple typed drying tasks can be handed off without pre-stops."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_rejects_mixed_stop_and_dry_tasks(self) -> None:
        """Mixed generic-stop and drying tasks remain ambiguous."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not can_prepare_clean_start(state)
        assert not await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_not_awaited()

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
        # This fixture exercises subscription renewal, not deferred map fetches.
        # Keep that background path suppressed so its mocked task creator does
        # not leave an unconsumed coroutine behind.
        c._map_fetch_pending = True
        c._last_display_map_resub = 0.0
        c._last_topic_subscribe = last_subscribe
        c._prev_working_status = MagicMock()
        c.active_clean_work_mode = None
        c.active_room_clean_settings = {}
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
