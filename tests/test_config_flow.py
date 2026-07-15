"""Tests for Narwal config flow -- covers HACS default listing requirements.

Mocks the homeassistant framework via ha_stubs so config_flow.py can be
imported and tested without a full HA installation.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal import _async_entry_updated  # noqa: E402
from custom_components.narwal.config_flow import (  # noqa: E402
    NarwalConfigFlow,
    NarwalOptionsFlow,
    _cloud_data_from_input,
)
from custom_components.narwal.const import (  # noqa: E402
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_REGION,
    DEFAULT_CLOUD_REGION,
)
from custom_components.narwal.narwal_client import NarwalConnectionError  # noqa: E402

AbortFlow = sys.modules["homeassistant.data_entry_flow"].AbortFlow


class TestNarwalConfigFlow:
    """Tests for NarwalConfigFlow.async_step_user branching logic."""

    def _make_flow(self) -> NarwalConfigFlow:
        """Create a NarwalConfigFlow with stubbed base-class methods."""
        flow = NarwalConfigFlow.__new__(NarwalConfigFlow)
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        return flow

    def test_cloud_password_without_email_is_not_stored(self) -> None:
        assert _cloud_data_from_input(
            {CONF_CLOUD_PASSWORD: "unused-password"}
        ) == {}

    async def test_show_form_when_no_input(self) -> None:
        """async_step_user with no input returns a form with step_id='user'."""
        flow = self._make_flow()
        await flow.async_step_user(user_input=None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "user"

    async def test_successful_setup_creates_entry(self) -> None:
        """async_step_user with valid input creates a config entry."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/QoEsI5qYXO"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "test_device_123"
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.100",
                    "port": 9002,
                    "model": "Narwal Flow",
                },
            )

        mock_client.connect.assert_awaited_once()
        mock_client.discover_device_id.assert_awaited_once()
        mock_client.get_device_info.assert_awaited_once()
        flow.async_set_unique_id.assert_awaited_once_with("test_device_123")
        flow._abort_if_unique_id_configured.assert_called_once()
        flow.async_create_entry.assert_called_once()
        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"]["host"] == "10.0.0.100"
        assert entry_kwargs["data"]["port"] == 9002
        assert entry_kwargs["data"]["device_id"] == "test_device_123"
        assert entry_kwargs["data"]["product_key"] == "QoEsI5qYXO"
        assert entry_kwargs["data"]["model"] == "Narwal Flow"
        mock_client.disconnect.assert_awaited_once()

    async def test_connection_error_shows_form_with_error(self) -> None:
        """async_step_user with connection failure returns form with cannot_connect."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.connect.side_effect = NarwalConnectionError("timeout")

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.200",
                    "port": 9002,
                    "model": "Narwal Flow",
                },
            )

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["errors"] == {"base": "cannot_connect"}
        assert mock_client.disconnect.await_count == 2

    async def test_duplicate_device_aborts(self) -> None:
        """async_step_user with duplicate unique_id aborts with already_configured."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/QoEsI5qYXO"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "duplicate_device"
        mock_client.get_device_info.return_value = mock_device_info

        flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")

        with (
            patch(
                "custom_components.narwal.config_flow.NarwalClient",
                return_value=mock_client,
            ),
            pytest.raises(AbortFlow, match="already_configured"),
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.100",
                    "port": 9002,
                    "model": "Narwal Flow",
                },
            )

        flow.async_set_unique_id.assert_awaited_once_with("duplicate_device")
        mock_client.disconnect.assert_awaited_once()

    async def test_auto_detect_model_uses_resolved_key(self) -> None:
        """async_step_user with auto-detect uses the resolved product key."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/DrzDKQ0MU8"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "auto_device_456"
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.50",
                    "port": 9002,
                    "model": "Other / Auto-detect",
                },
            )

        flow.async_create_entry.assert_called_once()
        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"]["product_key"] == "DrzDKQ0MU8"
        assert "Narwal DrzDKQ0MU8" in entry_kwargs["title"]


class TestNarwalOptionsUpdate:
    """Tests for config-entry option updates."""

    async def test_cloud_form_preserves_existing_options(self) -> None:
        """Saving cloud credentials keeps map options and an existing password."""
        entry = MagicMock()
        entry.options = {
            "show_room_labels": False,
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_PASSWORD: "saved-password",
        }
        flow = NarwalOptionsFlow()
        flow.config_entry = entry
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "new@example.com",
                CONF_CLOUD_PASSWORD: "",
            }
        )

        options = flow.async_create_entry.call_args.kwargs["data"]
        assert options["show_room_labels"] is False
        assert options[CONF_CLOUD_EMAIL] == "new@example.com"
        assert options[CONF_CLOUD_PASSWORD] == "saved-password"
        assert options[CONF_CLOUD_REGION] == DEFAULT_CLOUD_REGION

    async def test_cloud_credentials_reload_entry(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "narwal-entry"
        entry.data = {}
        entry.options = {
            CONF_CLOUD_EMAIL: "new@example.com",
            CONF_CLOUD_PASSWORD: "new-password",
        }
        entry.runtime_data.cloud_credentials = (None, None, DEFAULT_CLOUD_REGION)

        await _async_entry_updated(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with("narwal-entry")

    async def test_cloud_form_can_disable_existing_credentials(self) -> None:
        """Clearing the cloud email explicitly disables legacy credentials."""
        entry = MagicMock()
        entry.data = {
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_PASSWORD: "password",
        }
        entry.options = {"show_room_labels": False}
        flow = NarwalOptionsFlow()
        flow.config_entry = entry
        flow.hass = MagicMock()
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "",
                CONF_CLOUD_PASSWORD: "",
            }
        )

        options = flow.async_create_entry.call_args.kwargs["data"]
        assert options["show_room_labels"] is False
        assert options[CONF_CLOUD_EMAIL] == ""
        assert options[CONF_CLOUD_PASSWORD] == ""
        flow.hass.config_entries.async_update_entry.assert_called_once_with(
            entry,
            data={},
        )

    async def test_disabled_cloud_credentials_reload_entry(self) -> None:
        """Explicitly empty options override credentials stored in entry data."""
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "narwal-entry"
        entry.data = {
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_PASSWORD: "password",
        }
        entry.options = {
            CONF_CLOUD_EMAIL: "",
            CONF_CLOUD_PASSWORD: "",
        }
        entry.runtime_data.cloud_credentials = (
            "user@example.com",
            "password",
            DEFAULT_CLOUD_REGION,
        )

        await _async_entry_updated(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with("narwal-entry")

    async def test_map_options_do_not_reload_entry(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        entry = MagicMock()
        entry.data = {
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_PASSWORD: "password",
        }
        entry.options = {"show_room_labels": False}
        entry.runtime_data.cloud_credentials = (
            "user@example.com",
            "password",
            DEFAULT_CLOUD_REGION,
        )

        await _async_entry_updated(hass, entry)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_cloud_region_change_reloads_entry(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "narwal-entry"
        entry.data = {}
        entry.options = {
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_PASSWORD: "password",
            CONF_CLOUD_REGION: "us",
        }
        entry.runtime_data.cloud_credentials = (
            "user@example.com",
            "password",
            DEFAULT_CLOUD_REGION,
        )

        await _async_entry_updated(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with("narwal-entry")
