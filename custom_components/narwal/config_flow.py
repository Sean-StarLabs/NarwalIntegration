"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_PORT,
    DOMAIN,
    NARWAL_MODELS,
    NO_BROADCAST_PRODUCT_KEYS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=MODEL_OPTIONS[0]): vol.In(MODEL_OPTIONS),
    }
)

STEP_DEVICE_ID_DATA_SCHEMA = vol.Schema({vol.Required(CONF_DEVICE_ID): str})


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
            self._pending_user_input = user_input
            self._pending_product_key = product_key

            if product_key in NO_BROADCAST_PRODUCT_KEYS:
                return await self.async_step_device_id()

            topic_prefix = None if product_key == "auto" else f"/{product_key}"
            client = NarwalClient(host=host, port=port, topic_prefix=topic_prefix)
            try:
                await client.connect()
            except Exception as ex:
                _LOGGER.warning("Setup connection failed: %s: %s", type(ex).__name__, ex)
                errors["base"] = "cannot_connect"
            else:
                try:
                    await client.discover_device_id(timeout=15.0)
                    await client.drain_ws_buffer()
                    device_info = await client.get_device_info()
                except Exception as ex:
                    _LOGGER.info("Automatic Device ID discovery failed: %s", ex)
                    return await self.async_step_device_id()
                else:
                    return await self._async_create_entry(client, device_info.device_id)
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_device_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a manually supplied Device ID after discovery is unavailable."""
        return await self._async_step_device_id(user_input, "device_id")

    async def _async_step_device_id(
        self,
        user_input: dict[str, Any] | None,
        step_id: str,
    ) -> ConfigFlowResult:
        """Validate a manually supplied Device ID."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()
            setup = self._pending_user_input
            product_key = self._pending_product_key
            topic_prefix = None if product_key == "auto" else f"/{product_key}"
            client = NarwalClient(
                host=setup["host"],
                port=setup.get("port", DEFAULT_PORT),
                device_id=device_id,
                topic_prefix=topic_prefix,
            )
            try:
                await client.connect()
                if product_key == "auto":
                    await client.discover_device_id(timeout=15.0)
                    await client.drain_ws_buffer()
                device_info = await client.get_device_info()
            except Exception as ex:
                _LOGGER.warning("Device ID validation failed: %s: %s", type(ex).__name__, ex)
                errors["base"] = "cannot_connect"
            else:
                return await self._async_create_entry(client, device_info.device_id)
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_DEVICE_ID_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_create_entry(self, client: NarwalClient, device_id: str) -> ConfigFlowResult:
        """Create an entry from a locally validated robot identity."""
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        setup = self._pending_user_input
        product_key = self._pending_product_key
        resolved_key = client.topic_prefix.lstrip("/")
        model_label = setup[CONF_MODEL]
        return self.async_create_entry(
            title=model_label if product_key != "auto" else f"Narwal {resolved_key}",
            data={
                "host": setup["host"],
                "port": setup.get("port", DEFAULT_PORT),
                CONF_DEVICE_ID: device_id,
                CONF_PRODUCT_KEY: resolved_key,
                CONF_MODEL: model_label,
            },
        )
