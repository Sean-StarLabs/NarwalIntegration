"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_PORT,
    DOMAIN,
    MODEL_AUTO_LABEL,
    NARWAL_MODELS,
    model_label_for_product_key,
    NO_BROADCAST_PRODUCT_KEYS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())

# Pre-select auto-detect rather than the first model in the list. mDNS carries
# no model information, so whatever sits first is a guess, and accepting it is
# the normal path -- which is how a Flow 2 came to be called "Narwal Flow"
# (#81). Auto-detect reads the key off the robot and names the entry from that.
MODEL_DEFAULT = MODEL_AUTO_LABEL

# `NARWAL_7bb53c.local.` (DHCP/mDNS hostname) and the zeroconf instance name
# `_app_wss_server_7bb53c._narwal_sweeper._tcp.local.` both end in the last six
# hex characters of the robot's device_id.
_DISCOVERY_NAME_RE = re.compile(r"(?:narwal|app_wss_server)[_-]([0-9a-f]{6})", re.I)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=MODEL_DEFAULT): vol.In(MODEL_OPTIONS),
    }
)

CONF_RETRY_AUTO_DETECT = "retry_auto_detect"

# device_id is Optional so the retry checkbox can be submitted on its own. A
# blank id with retry unticked is rejected below rather than by the schema, so
# the user gets a translated error instead of a raw voluptuous message.
STEP_DEVICE_ID_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_DEVICE_ID, default=""): str,
        vol.Optional(CONF_RETRY_AUTO_DETECT, default=False): bool,
    }
)


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal vacuum."""

    VERSION = 2

    # Host pre-filled when zeroconf or DHCP finds the robot before the user
    # starts the flow. Class-level default: the flow object is also constructed
    # directly in tests, and there is nothing else to initialize.
    _discovered_host: str | None = None

    @staticmethod
    def _user_schema(default_host: str | None) -> vol.Schema:
        """User-step schema, pre-filling the host when we already know it."""
        if not default_host:
            return STEP_USER_DATA_SCHEMA
        return vol.Schema(
            {
                vol.Required("host", default=default_host): str,
                vol.Optional("port", default=DEFAULT_PORT): int,
                vol.Required(CONF_MODEL, default=MODEL_DEFAULT): vol.In(MODEL_OPTIONS),
            }
        )

    @staticmethod
    def _device_id_suffix(hostname: str) -> str | None:
        """Extract the six hex characters a robot puts in its discovery name.

        Verified on a Flow (AX12): device_id `71c53f01c14f49088338863e147bb53c`
        advertises `_app_wss_server_7bb53c._narwal_sweeper._tcp.local.` with
        hostname `NARWAL_7bb53c.local.` — the tail of the device_id in both.
        That's the only thing linking a discovery to a configured entry, whose
        unique_id is the full device_id read over the WebSocket.
        """
        match = _DISCOVERY_NAME_RE.search(hostname)
        return match.group(1).lower() if match else None

    async def _async_discovered(
        self, host: str, fallback_uid: str
    ) -> ConfigFlowResult:
        """Route a discovered robot into the user step, or abort if it's known.

        A configured entry's unique_id is the robot's device_id, which neither
        mDNS nor DHCP can see. Without a match, a robot added by hand would
        reappear as a "Discovered" card forever — so match on the device_id
        suffix in the discovery name, falling back to the address.
        """
        suffix = self._device_id_suffix(fallback_uid)
        for entry in self._async_current_entries(include_ignore=False):
            same_device = bool(suffix) and str(
                entry.data.get("device_id", "")
            ).lower().endswith(suffix)
            if not same_device and entry.data.get("host") != host:
                continue
            if same_device and entry.data.get("host") != host:
                # Same robot, new address — a DHCP renewal or a subnet move.
                # Repoint the entry instead of leaving it pointing at nothing.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "host": host}
                )
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(fallback_uid)
        self._abort_if_unique_id_configured(updates={"host": host})

        self._discovered_host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_user()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Pick up robots advertising `_narwal_sweeper._tcp.local.`.

        A robot advertises an instance named
        `_app_wss_server_<6hex>._narwal_sweeper._tcp.local.` with hostname
        `NARWAL_<6hex>.local.` on port 9002. Only the address is used here —
        the model isn't in the mDNS payload, so the user still picks it.
        """
        return await self._async_discovered(
            str(discovery_info.host), discovery_info.hostname.rstrip(".")
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Pick up robots by DHCP hostname — a backup for when mDNS is filtered.

        Plenty of home networks drop multicast across VLANs or on wireless
        isolation, and the robot is then invisible to zeroconf. The `narwal_*`
        hostname match in manifest.json still catches it — the robot announces
        itself as `NARWAL_<6hex>`, and HA lowercases hostnames before matching.
        """
        host = str(discovery_info.ip)
        return await self._async_discovered(host, discovery_info.hostname or host)

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

        # Keep whatever host we have: what the user just typed on a retry after
        # an error, otherwise the discovered address.
        default_host = (user_input or {}).get("host") or self._discovered_host
        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(default_host),
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
            if user_input.get(CONF_RETRY_AUTO_DETECT):
                # Automatic discovery can fail transiently — a robot in deep
                # sleep, or a first attempt that lands mid-reconnect. Without
                # this the step could only ever re-show itself, and because
                # Home Assistant resumes an in-progress discovery flow at its
                # current step, starting the flow again came straight back
                # here. Restarting Home Assistant was the only way out (#81).
                return await self.async_step_user()

            device_id = user_input.get(CONF_DEVICE_ID, "").strip()
            if not device_id:
                return self.async_show_form(
                    step_id=step_id,
                    data_schema=STEP_DEVICE_ID_DATA_SCHEMA,
                    errors={"base": "device_id_required"},
                )

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
        # The model label always describes the key the robot actually reported.
        #
        # CONF_PRODUCT_KEY below is `resolved_key` — read off the robot's own
        # topic — regardless of what was selected, so the stored key already
        # overrides the user. Letting the label disagree with it produced
        # entries carrying the Flow 2 key under the name "Narwal Flow" (#81):
        # the model selector defaults to the first option, discovery cannot
        # pre-select anything, and accepting that default silently mislabelled
        # a correctly configured robot.
        #
        # An unrecognised key keeps whatever the user chose, and auto-detect
        # falls back to showing the raw key — it is the only identifying thing
        # we have then, and it is what a bug report needs.
        resolved_label = model_label_for_product_key(resolved_key)
        model_label = resolved_label or setup[CONF_MODEL]
        if resolved_label:
            title = resolved_label
        elif product_key == "auto":
            title = f"Narwal {resolved_key}"
        else:
            title = model_label
        return self.async_create_entry(
            title=title,
            data={
                "host": setup["host"],
                "port": setup.get("port", DEFAULT_PORT),
                CONF_DEVICE_ID: device_id,
                CONF_PRODUCT_KEY: resolved_key,
                CONF_MODEL: model_label,
            },
        )
