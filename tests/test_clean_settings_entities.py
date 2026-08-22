"""Tests for the clean-settings select and number entities (#50).

Importing these modules at all is the regression guard for the bad
`RestoreSelect` import; the rest exercises the value round-trip, the live
mop-humidity setter, and the RestoreEntity/RestoreNumber restore paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.select import SelectEntity  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers.restore_state import RestoreEntity  # noqa: E402

from narwal_client.const import (  # noqa: E402
    CleaningRoute,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
    WorkingStatus,
)
from narwal_client import RoomCleanSettings  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402
from custom_components.narwal.coordinator import CleanSettings  # noqa: E402
from custom_components.narwal.number import NarwalPassesNumber  # noqa: E402
from custom_components.narwal.select import (  # noqa: E402
    LEGACY_SELECT_DESCRIPTIONS,
    LegacyNarwalSettingSelect,
    NarwalSelect,
    ROOM_SELECT_DESCRIPTIONS,
    RoomNarwalSettingSelect,
    SELECT_DESCRIPTIONS,
    async_setup_entry,
)

_DESCS = {d.key: d for d in SELECT_DESCRIPTIONS}
_LEGACY_DESCS = {d.setting_key: d for d in LEGACY_SELECT_DESCRIPTIONS}
_ROOM_DESCS = {d.setting_key: d for d in ROOM_SELECT_DESCRIPTIONS}


def _coordinator(
    *, settings: CleanSettings | None = None, state: object | None = None
) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1"}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    coord.client.state = MagicMock()
    coord.client.state.firmware_version = "1.0.0"
    coord.last_update_success = True
    coord.clean_settings = settings or CleanSettings()
    coord.room_clean_settings = {}
    coord.data = state
    coord._legacy_mode_option = "Vacuum and mop"

    def room_clean_settings_for(room_id: int) -> RoomCleanSettings:
        if room_id not in coord.room_clean_settings:
            coord.room_clean_settings[room_id] = RoomCleanSettings(
                work_mode=coord.clean_settings.work_mode,
                fan=coord.clean_settings.fan,
                water=coord.clean_settings.water,
                mop_strength=coord.clean_settings.mop_strength,
                passes=coord.clean_settings.passes,
                route=coord.clean_settings.route,
            )
        return coord.room_clean_settings[room_id]

    def set_room_clean_setting(room_id: int, attr: str, value) -> None:
        setattr(room_clean_settings_for(room_id), attr, value)

    coord.room_clean_settings_for.side_effect = room_clean_settings_for
    coord.set_room_clean_setting.side_effect = set_room_clean_setting
    return coord


def _state(
    working_status: WorkingStatus = WorkingStatus.DOCKED,
    *,
    recent: bool = False,
    returning: bool = False,
) -> MagicMock:
    state = MagicMock()
    state.working_status = working_status
    state.has_recent_active_working_status = recent
    state.is_returning = returning
    state.is_cleaning = working_status == WorkingStatus.CLEANING and not returning
    state.is_charging_to_resume = False
    state.is_station_active = False
    state.map_data = None
    return state


def test_select_bases_use_restore_entity() -> None:
    """The select restores via RestoreEntity (HA has no RestoreSelect)."""
    assert issubclass(NarwalSelect, RestoreEntity)
    assert issubclass(NarwalSelect, SelectEntity)


class TestNarwalSelect:
    def test_current_option_reflects_settings(self) -> None:
        coord = _coordinator(settings=CleanSettings(work_mode=WorkMode.MOP))
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        assert sel.current_option == "mop"

    async def test_select_option_stores_value(self) -> None:
        coord = _coordinator()
        sel = NarwalSelect(coord, _DESCS["mop_strength"])
        await sel.async_select_option("high")
        assert coord.clean_settings.mop_strength == MopStrengthLevel.HIGH
        assert sel.current_option == "high"

    async def test_water_applies_live_while_cleaning(self) -> None:
        coord = _coordinator(state=MagicMock(is_cleaning=True))
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])
        await sel.async_select_option("wet")
        assert coord.clean_settings.water == MopHumidity.WET
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)

    async def test_no_live_setter_when_not_cleaning(self) -> None:
        coord = _coordinator(state=MagicMock(is_cleaning=False))
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])
        await sel.async_select_option("dry")
        coord.client.set_mop_humidity.assert_not_awaited()

    async def test_restore_from_last_state(self) -> None:
        coord = _coordinator()
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        with patch.object(
            sel, "async_get_last_state", AsyncMock(return_value=MagicMock(state="mop"))
        ):
            await sel.async_added_to_hass()
        assert coord.clean_settings.work_mode == WorkMode.MOP

    async def test_restore_ignores_unknown_option(self) -> None:
        coord = _coordinator(settings=CleanSettings(work_mode=WorkMode.VACUUM))
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        with patch.object(
            sel, "async_get_last_state", AsyncMock(return_value=MagicMock(state="bogus"))
        ):
            await sel.async_added_to_hass()
        assert coord.clean_settings.work_mode == WorkMode.VACUUM

    def test_start_only_selects_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not NarwalSelect(coord, _DESCS["work_mode"]).available
        assert not NarwalSelect(coord, _DESCS["mop_strength"]).available
        assert NarwalSelect(coord, _DESCS["water"]).available


class TestLegacyNarwalSettingSelect:
    def test_start_only_settings_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        for key in ("mode", "passes", "scrub"):
            assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS[key]).available

    def test_live_settings_remain_available_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"]).available
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"]).available

    def test_suction_options_stay_stable_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        assert "AI" in sel.options
        assert "Standard" in sel.options

    async def test_ai_suction_rejected_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.client.set_fan_speed = AsyncMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        try:
            await sel.async_select_option("AI")
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("AI suction should not be selectable mid-clean")
        coord.client.set_fan_speed.assert_not_awaited()

    def test_route_is_available_when_idle(self) -> None:
        coord = _coordinator(state=_state())
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"]).available

    def test_route_is_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"]).available

    def test_mode_specific_settings_unavailable_when_not_applicable(self) -> None:
        coord = _coordinator(state=_state())
        coord._legacy_mode_option = "Vacuum"
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"]).available
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["scrub"]).available

        coord._legacy_mode_option = "Mop"
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"]).available

    async def test_select_option_refreshes_related_setting_entities(self) -> None:
        coord = _coordinator(state=_state())
        coord.async_update_listeners = MagicMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["mode"])
        await sel.async_select_option("Vacuum")
        coord.async_update_listeners.assert_called_once()

    async def test_route_select_stores_clean_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"])
        await sel.async_select_option("Standard")
        assert coord.clean_settings.route == CleaningRoute.STANDARD


class TestRoomNarwalSettingSelect:
    def test_current_option_reflects_room_settings(self) -> None:
        coord = _coordinator()
        coord.room_clean_settings[4] = RoomCleanSettings(
            work_mode=WorkMode.MOP,
            route=CleaningRoute.METICULOUS,
        )
        assert (
            RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["mode"]).current_option
            == "Mop"
        )

    async def test_select_option_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        await sel.async_select_option("Standard")
        assert coord.room_clean_settings[4].route == CleaningRoute.STANDARD

    async def test_select_passes_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["passes"])
        await sel.async_select_option("3")
        assert coord.room_clean_settings[4].passes == 3

    async def test_select_suction_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"])
        await sel.async_select_option("Strong")
        assert coord.room_clean_settings[4].fan == FanLevel.STRONG

    def test_mode_specific_settings_unavailable_when_not_applicable(self) -> None:
        coord = _coordinator(state=_state())
        coord.room_clean_settings[4] = RoomCleanSettings(work_mode=WorkMode.VACUUM)
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["water"]).available
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["scrub"]).available

        coord.room_clean_settings[4].work_mode = WorkMode.MOP
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"]).available

    def test_room_profiles_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["mode"]).available

    async def test_room_setting_entities_update_name_after_map_rename(self) -> None:
        coord = _coordinator()
        coord.client.state.map_data = MapData(
            rooms=[RoomInfo(room_id=4, name="Kitchen")]
        )
        entry = MagicMock()
        entry.runtime_data = coord
        added_entities = []
        listeners = []

        def add_entities(entities) -> None:
            added_entities.extend(entities)

        coord.async_add_listener.side_effect = lambda listener: listeners.append(listener)

        await async_setup_entry(MagicMock(), entry, add_entities)

        room_mode = next(
            entity
            for entity in added_entities
            if isinstance(entity, RoomNarwalSettingSelect)
            and entity.entity_description.setting_key == "mode"
        )
        assert room_mode._attr_name == "Kitchen mode"
        assert room_mode.extra_state_attributes["room_name"] == "Kitchen"

        coord.client.state.map_data = MapData(
            rooms=[RoomInfo(room_id=4, name="Pantry")]
        )
        listeners[0]()

        assert len(added_entities) == len(SELECT_DESCRIPTIONS) + len(
            LEGACY_SELECT_DESCRIPTIONS
        ) + len(ROOM_SELECT_DESCRIPTIONS)
        assert room_mode._attr_name == "Pantry mode"
        assert room_mode.extra_state_attributes["room_name"] == "Pantry"


class TestNarwalPassesNumber:
    def test_native_value_reflects_settings(self) -> None:
        coord = _coordinator(settings=CleanSettings(passes=2))
        assert NarwalPassesNumber(coord).native_value == 2

    def test_available_when_idle(self) -> None:
        coord = _coordinator(state=_state())
        assert NarwalPassesNumber(coord).available

    def test_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not NarwalPassesNumber(coord).available

    async def test_set_native_value_stores_int(self) -> None:
        coord = _coordinator()
        num = NarwalPassesNumber(coord)
        await num.async_set_native_value(3.0)
        assert coord.clean_settings.passes == 3

    async def test_restore_from_last_number_data(self) -> None:
        coord = _coordinator()
        num = NarwalPassesNumber(coord)
        data = MagicMock(native_value=2)
        with patch.object(num, "async_get_last_number_data", AsyncMock(return_value=data)):
            await num.async_added_to_hass()
        assert coord.clean_settings.passes == 2
