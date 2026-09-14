"""Tests for the integration entry point (custom_components.narwal.__init__)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

from custom_components.narwal import async_setup_entry  # noqa: E402


def _entry(host: str) -> MagicMock:
    entry = MagicMock()
    entry.data = {"host": host}
    entry.entry_id = "entry-id"
    return entry


async def test_unexpected_setup_failure_becomes_config_entry_not_ready() -> None:
    """#101: any startup exception must ask HA to retry, not mark the entry failed.

    A ValueError escaping coordinator.async_setup() left the entry in the
    setup-error state, which HA never retries; only delete + re-add recovered.
    """
    coordinator = MagicMock()
    coordinator.async_setup = AsyncMock(
        side_effect=ValueError("Port could not be cast to integer value")
    )

    with patch("custom_components.narwal.NarwalCoordinator", return_value=coordinator):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(MagicMock(), _entry("fd00::1"))
