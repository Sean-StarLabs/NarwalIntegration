"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CLOUD_REGIONS,
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_REGION,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DOMAIN,
    NARWAL_MODELS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())
PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=MODEL_OPTIONS[0]): vol.In(MODEL_OPTIONS),
        vol.Optional(CONF_CLOUD_EMAIL): str,
        vol.Optional(CONF_CLOUD_PASSWORD): PASSWORD_SELECTOR,
        vol.Optional(CONF_CLOUD_REGION, default=DEFAULT_CLOUD_REGION): vol.In(
            CLOUD_REGIONS
        ),
    }
)


def _cloud_data_from_input(user_input: dict[str, Any]) -> dict[str, str]:
    """Return non-empty cloud credential fields from form input."""
    cloud_data: dict[str, str] = {}
    cloud_email = user_input.get(CONF_CLOUD_EMAIL)
    cloud_password = user_input.get(CONF_CLOUD_PASSWORD)
    if cloud_email:
        cloud_data[CONF_CLOUD_EMAIL] = cloud_email
        if cloud_password:
            cloud_data[CONF_CLOUD_PASSWORD] = cloud_password
        cloud_data[CONF_CLOUD_REGION] = user_input.get(
            CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION
        )
    return cloud_data


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal vacuum."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step — user enters IP, port, and model."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            model_label = user_input[CONF_MODEL]
            product_key = NARWAL_MODELS[model_label]

            # Try the selected model key first for speed. Some Flow/Flow 2
            # units only wake on a sibling key before reporting their actual
            # product key, so fall back to auto-discovery before failing.
            topic_prefixes: list[str | None] = (
                [None] if product_key == "auto" else [f"/{product_key}", None]
            )

            last_error: Exception | None = None
            for topic_prefix in topic_prefixes:
                client = NarwalClient(
                    host=host,
                    port=port,
                    topic_prefix=topic_prefix,
                )
                try:
                    await client.connect()
                    # Discover device_id from broadcast, then query info
                    await client.discover_device_id(timeout=15.0)
                    # Drain any stale field5 responses left in the WebSocket
                    # buffer from discover's wake probes before sending a
                    # real command
                    await client.drain_ws_buffer()
                    device_info = await client.get_device_info()
                except Exception as ex:
                    last_error = ex
                    _LOGGER.debug(
                        "Setup probe failed with prefix %s: %s: %s",
                        topic_prefix or "auto",
                        type(ex).__name__,
                        ex,
                    )
                    await client.disconnect()
                    continue

                try:
                    device_id = device_info.device_id
                    await self.async_set_unique_id(device_id)
                    self._abort_if_unique_id_configured()

                    # Use the product key that actually worked (may have been
                    # auto-detected during discovery even if user picked "auto")
                    resolved_key = client.topic_prefix.lstrip("/")

                    return self.async_create_entry(
                        title=model_label if product_key != "auto" else f"Narwal {resolved_key}",
                        data={
                            "host": host,
                            "port": port,
                            "device_id": device_id,
                            CONF_PRODUCT_KEY: resolved_key,
                            CONF_MODEL: model_label,
                            **_cloud_data_from_input(user_input),
                        },
                    )
                finally:
                    await client.disconnect()

            if last_error is not None:
                _LOGGER.warning(
                    "Setup failed: %s: %s",
                    type(last_error).__name__,
                    last_error,
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return NarwalOptionsFlow()


class NarwalOptionsFlow(OptionsFlow):
    """Handle Narwal options."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage Narwal cloud options."""
        if user_input is not None:
            options = dict(self.config_entry.options or {})
            cloud_email = user_input.get(CONF_CLOUD_EMAIL, "").strip()
            cloud_password = user_input.get(CONF_CLOUD_PASSWORD, "")
            cloud_region = user_input.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION)
            options[CONF_CLOUD_EMAIL] = cloud_email
            options[CONF_CLOUD_REGION] = cloud_region
            if not cloud_email:
                options[CONF_CLOUD_PASSWORD] = ""
                entry_data = dict(self.config_entry.data)
                entry_data.pop(CONF_CLOUD_EMAIL, None)
                entry_data.pop(CONF_CLOUD_PASSWORD, None)
                entry_data.pop(CONF_CLOUD_REGION, None)
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=entry_data,
                )
            elif cloud_password:
                options[CONF_CLOUD_PASSWORD] = cloud_password
            return self.async_create_entry(
                title="",
                data=options,
            )

        entry_options = self.config_entry.options or {}
        entry_data = self.config_entry.data
        suggested_email = entry_options.get(
            CONF_CLOUD_EMAIL,
            entry_data.get(CONF_CLOUD_EMAIL, ""),
        )
        suggested_region = entry_options.get(
            CONF_CLOUD_REGION,
            entry_data.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CLOUD_EMAIL,
                        description={"suggested_value": suggested_email},
                    ): str,
                    vol.Optional(CONF_CLOUD_PASSWORD): PASSWORD_SELECTOR,
                    vol.Optional(
                        CONF_CLOUD_REGION,
                        default=suggested_region,
                    ): vol.In(CLOUD_REGIONS),
                }
            ),
        )
