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

from custom_components.narwal.cloud import NarwalCloudError  # noqa: E402
from custom_components.narwal.config_flow import (  # noqa: E402
    MODEL_DEFAULT,
    MODEL_OPTIONS,
    NarwalConfigFlow,
    NarwalOptionsFlow,
)
from custom_components.narwal.const import (  # noqa: E402
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_ID,
    CONF_CLOUD_REGION,
    CONF_MODEL,
    DEFAULT_CLOUD_REGION,
    MODEL_AUTO_LABEL,
    NARWAL_MODELS,
    NO_BROADCAST_PRODUCT_KEYS,
)
from custom_components.narwal.narwal_client import NarwalConnectionError  # noqa: E402

AbortFlow = sys.modules["homeassistant.data_entry_flow"].AbortFlow
DhcpServiceInfo = sys.modules[
    "homeassistant.helpers.service_info.dhcp"
].DhcpServiceInfo
ZeroconfServiceInfo = sys.modules[
    "homeassistant.helpers.service_info.zeroconf"
].ZeroconfServiceInfo


class TestNarwalConfigFlow:
    """Tests for NarwalConfigFlow.async_step_user branching logic."""

    def _make_flow(self) -> NarwalConfigFlow:
        """Create a NarwalConfigFlow with stubbed base-class methods."""
        flow = NarwalConfigFlow.__new__(NarwalConfigFlow)
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.hass = MagicMock()
        return flow

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
        assert entry_kwargs["data"][CONF_CLOUD_PRODUCT_ID] == "QoEsI5qYXO"
        assert entry_kwargs["data"]["model"] == "Narwal Flow"
        mock_client.disconnect.assert_awaited_once()

    async def test_invalid_cloud_credentials_show_error(self) -> None:
        """Paired cloud credentials are authenticated before being stored."""
        flow = self._make_flow()

        with patch("custom_components.narwal.config_flow.NarwalCloudClient") as cloud:
            cloud.return_value.async_login = AsyncMock(
                side_effect=NarwalCloudError("bad login")
            )
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.100",
                    "port": 9002,
                    "model": "Narwal Flow",
                    CONF_CLOUD_EMAIL: "owner@example.com",
                    CONF_CLOUD_PASSWORD: "wrong",
                    CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
                },
            )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_cannot_connect"
        }
        flow.async_create_entry.assert_not_called()

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
        mock_client.disconnect.assert_awaited_once()

    async def test_cloud_email_requires_password(self) -> None:
        """Cloud setup should not silently store unusable partial credentials."""
        flow = self._make_flow()

        await flow.async_step_user(
            user_input={
                "host": "10.0.0.100",
                "port": 9002,
                "model": "Narwal Flow",
                CONF_CLOUD_EMAIL: "user@example.com",
            },
        )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_credentials_incomplete"
        }

    async def test_cloud_credential_error_preserves_user_form_defaults(self) -> None:
        """Retry form should keep the selected model/region, not reset to defaults."""
        flow = self._make_flow()
        user_input = {
            "host": "10.0.0.100",
            "port": 9002,
            "model": "Narwal Freo Z Ultra (CX7)",
            CONF_CLOUD_EMAIL: "user@example.com",
            CONF_CLOUD_REGION: "de",
        }

        with patch.object(
            NarwalConfigFlow,
            "_user_schema",
            return_value="schema",
        ) as schema:
            await flow.async_step_user(user_input=user_input)

        schema.assert_called_once_with("10.0.0.100", user_input)
        assert flow.async_show_form.call_args.kwargs["data_schema"] == "schema"

    async def test_cloud_password_requires_email(self) -> None:
        """A password without an email is also incomplete."""
        flow = self._make_flow()

        await flow.async_step_user(
            user_input={
                "host": "10.0.0.100",
                "port": 9002,
                "model": "Narwal Flow",
                CONF_CLOUD_PASSWORD: "secret",
            },
        )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_credentials_incomplete"
        }

    async def test_duplicate_device_aborts(self) -> None:
        """async_step_user with duplicate unique_id aborts with already_configured."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/QoEsI5qYXO"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "duplicate_device"
        mock_client.get_device_info.return_value = mock_device_info

        flow._abort_if_unique_id_configured.side_effect = AbortFlow(
            "already_configured"
        )

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ), pytest.raises(AbortFlow, match="already_configured"):
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
        # DrzDKQ0MU8 is a model we know, so the entry is named after it rather
        # than after the raw key it was discovered by (#81).
        assert entry_kwargs["title"] == "Narwal Freo Z10 Ultra"
        assert entry_kwargs["data"][CONF_MODEL] == "Narwal Freo Z10 Ultra"
        assert entry_kwargs["data"][CONF_CLOUD_PRODUCT_ID] == "DrzDKQ0MU8"

    async def test_auto_detect_unknown_key_keeps_the_key_in_the_title(self) -> None:
        """An unrecognised product key stays visible — it is all we know."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/zzzzUNKNOWN"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "auto_device_789"
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

        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["title"] == "Narwal zzzzUNKNOWN"
        assert entry_kwargs["data"][CONF_MODEL] == "Other / Auto-detect"

    async def test_model_label_follows_the_key_the_robot_reported(self) -> None:
        """@DeNo64's case (#81): a Flow 2 accepted at the "Narwal Flow" default.

        The selector defaults to the first option and discovery cannot
        pre-select anything, so accepting the default is the common path.
        CONF_PRODUCT_KEY is already taken from the robot rather than the user,
        so letting the label disagree stored the Flow 2 key under the name
        "Narwal Flow" — a correctly configured robot, mislabelled.

        This replaces an earlier test asserting the opposite. Respecting the
        selected label while overriding the selected key is the inconsistency
        that caused the bug, not a feature worth protecting.
        """
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/QxMSPG6VSO"  # Narwal Flow 2
        mock_device_info = MagicMock()
        mock_device_info.device_id = "flow2_device"
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.50",
                    "port": 9002,
                    "model": "Narwal Flow",  # the default, left unchanged
                }
            )

        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"]["product_key"] == "QxMSPG6VSO"
        assert entry_kwargs["data"][CONF_MODEL] == "Narwal Flow 2"
        assert entry_kwargs["title"] == "Narwal Flow 2"

    async def test_unknown_key_keeps_the_model_the_user_chose(self) -> None:
        """Nothing to correct with, so the user's choice stands."""
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/zzzzUNKNOWN"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "unknown_device"
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.50",
                    "port": 9002,
                    "model": "Narwal Flow",
                }
            )

        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"][CONF_MODEL] == "Narwal Flow"
        assert entry_kwargs["title"] == "Narwal Flow"

    async def test_alternate_flow2_key_is_still_named_flow_2(self) -> None:
        """A model's other product keys name the entry too (#81).

        @DeNo64's Flow 2 reports `mkbqaprvrb`, not the `QxMSPG6VSO` the
        selector sends. The label lookup was built by reversing the selector
        dict, which holds exactly one key per model, so every other key a model
        ships resolved to no label at all. His robot worked perfectly -- 28
        entities on firmware v01.09.08.00 -- and was called "Narwal Flow"
        purely because its key was absent.

        This is why v1.0.7 looked like it had failed: the label did follow the
        resolved key, and the resolved key had no label.
        """
        flow = self._make_flow()

        mock_client = AsyncMock()
        mock_client.topic_prefix = "/mkbqaprvrb"
        mock_device_info = MagicMock()
        mock_device_info.device_id = "deno64_device"
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.50",
                    "port": 9002,
                    "model": MODEL_AUTO_LABEL,
                }
            )

        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"]["product_key"] == "mkbqaprvrb"
        assert entry_kwargs["data"][CONF_MODEL] == "Narwal Flow 2"
        assert entry_kwargs["title"] == "Narwal Flow 2"

    def test_model_selector_default_is_auto_detect_and_selectable(self) -> None:
        """The pre-selected model must not be a guess (#81).

        Discovery carries no model information, so whichever model sits first
        in the list is asserted rather than known, and accepting it is the
        normal path -- that is how a Flow 2 came to be called "Narwal Flow".

        Also pins the default to a real option: the schema validates it with
        `vol.In(MODEL_OPTIONS)`, so a default that is not in the list makes the
        form unusable rather than merely wrong. The schema objects themselves
        cannot be introspected here because ha_stubs replaces voluptuous with a
        MagicMock; that the flow honours the resolved key is covered by
        test_alternate_flow2_key_is_still_named_flow_2.
        """
        assert MODEL_DEFAULT == MODEL_AUTO_LABEL
        assert MODEL_DEFAULT in MODEL_OPTIONS
        assert NARWAL_MODELS[MODEL_DEFAULT] == "auto"

    async def test_device_id_step_can_retry_auto_detection(self) -> None:
        """The manual step is not a dead end — it can hand back to auto-detect.

        Home Assistant resumes an in-progress discovery flow at its current
        step, so before this the only escape was restarting Home Assistant.
        """
        flow = self._make_flow()
        flow._pending_user_input = {"host": "10.0.0.50", "port": 9002,
                                    CONF_MODEL: "Other / Auto-detect"}
        flow._pending_product_key = "auto"

        with patch(
            "custom_components.narwal.config_flow.NarwalClient"
        ) as client_class:
            await flow.async_step_device_id(
                user_input={"device_id": "", "retry_auto_detect": True}
            )

        # Hands back to the user form rather than connecting with a blank id.
        client_class.assert_not_called()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "user"

    async def test_blank_device_id_is_rejected_without_connecting(self) -> None:
        """A blank id with retry unticked is a translated error, not a crash."""
        flow = self._make_flow()
        flow._pending_user_input = {"host": "10.0.0.50", "port": 9002,
                                    CONF_MODEL: "Other / Auto-detect"}
        flow._pending_product_key = "auto"

        with patch(
            "custom_components.narwal.config_flow.NarwalClient"
        ) as client_class:
            await flow.async_step_device_id(
                user_input={"device_id": "   ", "retry_auto_detect": False}
            )

        client_class.assert_not_called()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "device_id"
        assert call_kwargs["errors"] == {"base": "device_id_required"}

    async def test_non_broadcast_model_opens_device_id_step(self) -> None:
        """A non-broadcast model requests its identity without connecting."""
        flow = self._make_flow()
        model = next(
            label
            for label, product_key in NARWAL_MODELS.items()
            if product_key in NO_BROADCAST_PRODUCT_KEYS
        )

        with patch("custom_components.narwal.config_flow.NarwalClient") as client_class:
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.70",
                    "port": 9002,
                    "model": model,
                }
            )

        client_class.assert_not_called()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "device_id"
        assert call_kwargs["errors"] == {}

    async def test_non_broadcast_device_id_skips_discovery(self) -> None:
        """A non-broadcast model validates an exactly addressed request."""
        flow = self._make_flow()
        device_id = "0123456789abcdef0123456789abcdef"
        model, product_key = next(
            (label, key)
            for label, key in NARWAL_MODELS.items()
            if key in NO_BROADCAST_PRODUCT_KEYS
        )
        mock_client = AsyncMock()
        mock_client.topic_prefix = f"/{product_key}"
        mock_device_info = MagicMock()
        mock_device_info.device_id = device_id
        mock_client.get_device_info.return_value = mock_device_info

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ) as client_class:
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.70",
                    "port": 9002,
                    "model": model,
                }
            )
            await flow.async_step_device_id(user_input={"device_id": f" {device_id} "})

        client_class.assert_called_once_with(
            host="10.0.0.70",
            port=9002,
            device_id=device_id,
            topic_prefix=f"/{product_key}",
        )
        mock_client.connect.assert_awaited_once()
        mock_client.discover_device_id.assert_not_awaited()
        mock_client.drain_ws_buffer.assert_not_awaited()
        mock_client.get_device_info.assert_awaited_once()
        entry_kwargs = flow.async_create_entry.call_args.kwargs
        assert entry_kwargs["data"]["device_id"] == device_id
        assert entry_kwargs["data"]["product_key"] == product_key
        assert entry_kwargs["data"][CONF_CLOUD_PRODUCT_ID] == "J5"
        mock_client.disconnect.assert_awaited_once()

    async def test_failed_auto_discovery_opens_device_id_step(self) -> None:
        """Any model can fall back to an exactly addressed setup request."""
        flow = self._make_flow()
        mock_client = AsyncMock()
        mock_client.discover_device_id.side_effect = TimeoutError

        with patch(
            "custom_components.narwal.config_flow.NarwalClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(
                user_input={
                    "host": "10.0.0.80",
                    "port": 9002,
                    "model": "Narwal Flow",
                }
            )

        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "device_id"
        mock_client.disconnect.assert_awaited_once()


class TestNarwalOptionsFlow:
    """Tests for Narwal cloud options."""

    def _make_flow(
        self,
        *,
        data: dict | None = None,
        options: dict | None = None,
    ) -> NarwalOptionsFlow:
        """Create an options flow with stubbed base-class methods."""
        flow = NarwalOptionsFlow()
        flow.config_entry = MagicMock()
        flow.config_entry.data = data or {}
        flow.config_entry.options = options or {}
        flow.hass = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        return flow

    async def test_changed_cloud_email_requires_password(self) -> None:
        """Changing account email must not reuse the prior account password."""
        flow = self._make_flow(
            data={
                CONF_CLOUD_EMAIL: "old@example.com",
                CONF_CLOUD_PASSWORD: "old-pass",
            }
        )

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "new@example.com",
                CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
            }
        )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_credentials_incomplete"
        }
        flow.async_create_entry.assert_not_called()

    async def test_unchanged_cloud_email_preserves_existing_password(self) -> None:
        """Leaving the password blank preserves credentials for the same account."""
        flow = self._make_flow(
            data={
                CONF_CLOUD_EMAIL: "old@example.com",
                CONF_CLOUD_PASSWORD: "old-pass",
            }
        )

        with patch("custom_components.narwal.config_flow.NarwalCloudClient") as cloud:
            cloud.return_value.async_login = AsyncMock()
            await flow.async_step_init(
                {
                    CONF_CLOUD_EMAIL: "old@example.com",
                    CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
                }
            )

        flow.async_create_entry.assert_called_once()
        flow.async_show_form.assert_not_called()

    async def test_cloud_email_without_existing_password_is_rejected(self) -> None:
        """An options email without any password source is incomplete."""
        flow = self._make_flow()

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "user@example.com",
                CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
            }
        )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_credentials_incomplete"
        }
        flow.async_create_entry.assert_not_called()

    async def test_cloud_password_without_email_is_rejected(self) -> None:
        """A password without an email must not silently disable cloud access."""
        flow = self._make_flow(
            data={
                CONF_CLOUD_EMAIL: "old@example.com",
                CONF_CLOUD_PASSWORD: "old-pass",
            }
        )

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "",
                CONF_CLOUD_PASSWORD: "new-pass",
                CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
            }
        )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_credentials_incomplete"
        }
        flow.async_create_entry.assert_not_called()

    async def test_disabling_cloud_access_updates_options_once(self) -> None:
        """Blank options override legacy credentials without a second reload."""
        flow = self._make_flow(
            data={
                CONF_CLOUD_EMAIL: "old@example.com",
                CONF_CLOUD_PASSWORD: "old-pass",
                CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
            }
        )

        await flow.async_step_init(
            {
                CONF_CLOUD_EMAIL: "",
                CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
            }
        )

        flow.hass.config_entries.async_update_entry.assert_not_called()
        flow.async_create_entry.assert_called_once()
        assert flow.async_create_entry.call_args.kwargs["data"] == {
            CONF_CLOUD_EMAIL: "",
            CONF_CLOUD_PASSWORD: "",
            CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
        }

    async def test_invalid_cloud_options_credentials_are_rejected(self) -> None:
        """Options flow validates a newly provided cloud password."""
        flow = self._make_flow()

        with patch("custom_components.narwal.config_flow.NarwalCloudClient") as cloud:
            cloud.return_value.async_login = AsyncMock(
                side_effect=NarwalCloudError("bad login")
            )
            await flow.async_step_init(
                {
                    CONF_CLOUD_EMAIL: "user@example.com",
                    CONF_CLOUD_PASSWORD: "wrong",
                    CONF_CLOUD_REGION: DEFAULT_CLOUD_REGION,
                }
            )

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "cloud_cannot_connect"
        }
        flow.async_create_entry.assert_not_called()


class TestDiscovery:
    """zeroconf / DHCP discovery routing into the user step."""

    def _make_flow(self, entries: list | None = None) -> NarwalConfigFlow:
        """Flow with the discovery-relevant base-class methods stubbed."""
        flow = NarwalConfigFlow.__new__(NarwalConfigFlow)
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_abort = MagicMock(
            side_effect=lambda reason: {"type": "abort", "reason": reason}
        )
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow._async_current_entries = MagicMock(return_value=entries or [])
        flow.context = {}
        flow.hass = MagicMock()
        return flow

    @staticmethod
    def _entry(host: str, device_id: str = "") -> MagicMock:
        entry = MagicMock()
        entry.data = {"host": host, "device_id": device_id}
        return entry

    async def test_zeroconf_routes_to_user_step_with_host_prefilled(self) -> None:
        """A discovered robot lands on the user form with its address filled in."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(
                host="10.0.0.112",
                hostname="NARWAL_8d5298.local.",
                port=9002,
            )
        )

        assert flow._discovered_host == "10.0.0.112"
        assert flow.context["title_placeholders"] == {"host": "10.0.0.112"}
        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "user"

    async def test_zeroconf_uses_hostname_without_trailing_dot_as_unique_id(self) -> None:
        """The mDNS trailing dot must not end up in the unique_id."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_8d5298.local.")
        )

        flow.async_set_unique_id.assert_awaited_once_with("NARWAL_8d5298.local")

    async def test_dhcp_routes_to_user_step(self) -> None:
        """DHCP discovery is the fallback path when multicast is filtered."""
        flow = self._make_flow()

        await flow.async_step_dhcp(
            DhcpServiceInfo(
                ip="10.0.0.112",
                hostname="NARWAL_8d5298",
                macaddress="809d658d5298",
            )
        )

        assert flow._discovered_host == "10.0.0.112"
        flow.async_set_unique_id.assert_awaited_once_with("NARWAL_8d5298")

    async def test_dhcp_without_hostname_falls_back_to_ip(self) -> None:
        """A lease with no hostname still needs some unique_id."""
        flow = self._make_flow()

        await flow.async_step_dhcp(DhcpServiceInfo(ip="10.0.0.112", hostname=""))

        flow.async_set_unique_id.assert_awaited_once_with("10.0.0.112")

    async def test_known_host_aborts_without_showing_a_card(self) -> None:
        """A robot added by hand must not reappear as a Discovered card.

        Its entry's unique_id is the device_id, which discovery can't see, so
        the host is the only thing the two paths have in common.
        """
        flow = self._make_flow(entries=[self._entry("10.0.0.112")])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_8d5298.local.")
        )

        assert result["reason"] == "already_configured"
        flow.async_show_form.assert_not_called()
        flow.async_set_unique_id.assert_not_awaited()

    async def test_other_host_still_offered(self) -> None:
        """A second robot is not suppressed by the first one's entry."""
        flow = self._make_flow(entries=[self._entry("10.0.0.112")])

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.113", hostname="NARWAL_aabbcc.local.")
        )

        flow.async_show_form.assert_called_once()
        assert flow._discovered_host == "10.0.0.113"

    async def test_rediscovery_at_a_new_address_updates_the_entry(self) -> None:
        """Hostname is the discovery id, so a DHCP renewal updates the host."""
        flow = self._make_flow()

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.150", hostname="NARWAL_8d5298.local.")
        )

        flow._abort_if_unique_id_configured.assert_called_once_with(
            updates={"host": "10.0.0.150"}
        )

    async def test_manually_added_robot_matched_by_device_id_suffix(self) -> None:
        """A hand-added entry is recognised by the device_id tail in the name.

        Verified on hardware: device_id 71c53f01…147bb53c advertises as
        NARWAL_7bb53c.local. — the only link between the two paths.
        """
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.112", hostname="NARWAL_7bb53c.local.")
        )

        assert result["reason"] == "already_configured"
        flow.async_show_form.assert_not_called()

    async def test_known_device_at_new_address_repoints_the_entry(self) -> None:
        """Same robot, new IP: update the entry rather than orphan it."""
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        result = await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.199", hostname="NARWAL_7bb53c.local.")
        )

        assert result["reason"] == "already_configured"
        flow.hass.config_entries.async_update_entry.assert_called_once()
        updated = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated["host"] == "10.0.0.199"
        assert updated["device_id"] == "71c53f01c14f49088338863e147bb53c"

    async def test_a_different_robot_is_not_matched(self) -> None:
        """A non-matching suffix at a different address is a new device."""
        entry = self._entry(
            "10.0.0.112", device_id="71c53f01c14f49088338863e147bb53c"
        )
        flow = self._make_flow(entries=[entry])

        await flow.async_step_zeroconf(
            ZeroconfServiceInfo(host="10.0.0.113", hostname="NARWAL_aabbcc.local.")
        )

        flow.async_show_form.assert_called_once()
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_device_id_suffix_parsing(self) -> None:
        """Both discovery name shapes yield the suffix; other names yield None."""
        parse = NarwalConfigFlow._device_id_suffix
        assert parse("NARWAL_7bb53c.local.") == "7bb53c"
        assert parse("narwal_7BB53C") == "7bb53c"
        assert parse("_app_wss_server_7bb53c._narwal_sweeper._tcp.local.") == "7bb53c"
        assert parse("10.0.0.112") is None
        assert parse("some-other-vacuum.local.") is None

    async def test_manual_flow_is_unaffected(self) -> None:
        """With no discovery, the user step shows the plain schema."""
        flow = self._make_flow()

        await flow.async_step_user(user_input=None)

        assert flow._discovered_host is None
        flow.async_show_form.assert_called_once()
