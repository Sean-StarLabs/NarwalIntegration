"""Tests for Narwal action buttons."""

from __future__ import annotations

from unittest.mock import MagicMock

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.button import (  # noqa: E402
    BUTTON_DESCRIPTIONS,
    NarwalActionButton,
)


_DESCS = {d.key: d for d in BUTTON_DESCRIPTIONS}


def _coordinator(*, is_docked: bool) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1"}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    coord.client.state = MagicMock()
    coord.client.state.firmware_version = "1.0.0"
    coord.last_update_success = True
    coord.data = MagicMock(is_docked=is_docked)
    return coord


def test_station_button_available_away_from_dock() -> None:
    coord = _coordinator(is_docked=False)
    button = NarwalActionButton(coord, _DESCS["empty_dustbin"])
    assert button.available
