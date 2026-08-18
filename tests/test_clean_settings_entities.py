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
from homeassistant.helpers.restore_state import RestoreEntity  # noqa: E402

from narwal_client.const import (  # noqa: E402
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
    WorkingStatus,
)
from custom_components.narwal.coordinator import CleanSettings  # noqa: E402
from custom_components.narwal.number import NarwalPassesNumber  # noqa: E402
from custom_components.narwal.select import (  # noqa: E402
    LEGACY_SELECT_DESCRIPTIONS,
    LegacyNarwalSettingSelect,
    NarwalSelect,
    SELECT_DESCRIPTIONS,
)

_DESCS = {d.key: d for d in SELECT_DESCRIPTIONS}
_LEGACY_DESCS = {d.setting_key: d for d in LEGACY_SELECT_DESCRIPTIONS}


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
    coord.data = state
    coord._legacy_mode_option = "Vacuum and mop"
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

    def test_suction_removes_ai_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        assert "AI" not in sel.options
        assert "Standard" in sel.options

    def test_route_is_hidden_until_supported_by_payload(self) -> None:
        coord = _coordinator(state=_state())
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


class TestNarwalPassesNumber:
    def test_native_value_reflects_settings(self) -> None:
        coord = _coordinator(settings=CleanSettings(passes=2))
        assert NarwalPassesNumber(coord).native_value == 2

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
