"""Tests for Narwal config-entry migrations."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal import (  # noqa: E402
    _async_remove_legacy_replaced_sensors,
    async_migrate_entry,
)
from custom_components.narwal.const import CONF_DEVICE_ID  # noqa: E402


async def test_migration_removes_legacy_replaced_sensor_registry_entries() -> None:
    """Old standalone task sensors are removed once native entities replace them."""
    hass = MagicMock()
    entry = MagicMock()
    entry.version = 2
    entry.minor_version = 1
    entry.data = {CONF_DEVICE_ID: "test_device"}
    entry.entry_id = "entry-id"

    registry = MagicMock()
    entity_ids_by_unique_id = {
        "test_device_current_room": "sensor.test_current_room",
        "test_device_task_status": "sensor.test_status",
    }
    registry.async_get_entity_id.side_effect = (
        lambda _domain, _platform, unique_id: entity_ids_by_unique_id.get(unique_id)
    )
    registry.async_get.return_value = SimpleNamespace(platform="narwal")

    with patch(
        "custom_components.narwal.er.async_get",
        return_value=registry,
    ), patch("custom_components.narwal.er.async_entries_for_config_entry", return_value=()):
        assert await async_migrate_entry(hass, entry)

    registry.async_get_entity_id.assert_any_call(
        "sensor",
        "narwal",
        "test_device_task_status",
    )
    registry.async_get_entity_id.assert_any_call(
        "sensor",
        "narwal",
        "test_device_current_room",
    )
    assert registry.async_remove.call_count == 2
    registry.async_remove.assert_any_call("sensor.test_current_room")
    registry.async_remove.assert_any_call("sensor.test_status")
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        minor_version=2,
    )


def test_legacy_replaced_sensor_cleanup_scans_config_entry_entities() -> None:
    """Renamed legacy sensors are still removed by unique_id."""
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {CONF_DEVICE_ID: "test_device"}
    entry.entry_id = "entry-id"
    stale = SimpleNamespace(
        domain="sensor",
        platform="narwal",
        unique_id="test_device_task_progress",
        entity_id="sensor.renamed_progress",
    )

    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    registry.async_get.return_value = stale

    with patch(
        "custom_components.narwal.er.async_get",
        return_value=registry,
    ), patch(
        "custom_components.narwal.er.async_entries_for_config_entry",
        return_value=(stale,),
    ):
        _async_remove_legacy_replaced_sensors(hass, entry)

    registry.async_remove.assert_called_once_with("sensor.renamed_progress")
