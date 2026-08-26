"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .cloud import NarwalCloudClient, NarwalCloudError
from .const import (
    CLOUD_REGIONS,
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_ID,
    CONF_CLOUD_REGION,
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DOMAIN,
    NARWAL_MODELS,
    NO_BROADCAST_PRODUCT_KEYS,
    cloud_product_id_for_product_key,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())
PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# `NARWAL_7bb53c.local.` (DHCP/mDNS hostname) and the zeroconf instance name
# `_app_wss_server_7bb53c._narwal_sweeper._tcp.local.` both end in the last six
# hex characters of the robot's device_id.
_DISCOVERY_NAME_RE = re.compile(r"(?:narwal|app_wss_server)[_-]([0-9a-f]{6})", re.I)

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

STEP_DEVICE_ID_DATA_SCHEMA = vol.Schema({vol.Required(CONF_DEVICE_ID): str})


def _cloud_data_from_input(user_input: dict[str, Any]) -> dict[str, str]:
    """Return non-empty cloud credential fields from form input."""
    cloud_data: dict[str, str] = {}
    cloud_email = user_input.get(CONF_CLOUD_EMAIL, "").strip()
    cloud_password = user_input.get(CONF_CLOUD_PASSWORD, "")
    if cloud_email:
        cloud_data[CONF_CLOUD_EMAIL] = cloud_email
        cloud_data[CONF_CLOUD_PASSWORD] = cloud_password
        cloud_data[CONF_CLOUD_REGION] = user_input.get(
            CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION
        )
    return cloud_data


def _cloud_credentials_error(email: str, password: str) -> str | None:
    """Return an error key when only one cloud credential field is set."""
    if bool(email.strip()) == bool(password):
        return None
    return "cloud_credentials_incomplete"


async def _async_validate_cloud_credentials(
    hass,
    *,
    email: str,
    password: str,
    region: str,
) -> str | None:
    """Return an error key if cloud credentials cannot authenticate."""
    if not email.strip() and not password:
        return None
    if error := _cloud_credentials_error(email, password):
        return error
    try:
        await NarwalCloudClient(
            hass,
            email=email.strip(),
            password=password,
            region=region,
        ).async_login()
    except (NarwalCloudError, ValueError) as err:
        _LOGGER.warning("Narwal cloud login validation failed: %s", err)
        return "cloud_cannot_connect"
    return None


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal vacuum."""

    VERSION = 2
    MINOR_VERSION = 2

    # Host pre-filled when zeroconf or DHCP finds the robot before the user
    # starts the flow. Class-level default: the flow object is also constructed
    # directly in tests, and there is nothing else to initialize.
    _discovered_host: str | None = None

    @staticmethod
    def _user_schema(
        default_host: str | None,
        defaults: dict[str, Any] | None = None,
    ) -> vol.Schema:
        """User-step schema, pre-filling the host when we already know it."""
        defaults = defaults or {}
        if not default_host and not defaults:
            return STEP_USER_DATA_SCHEMA
        host_default = defaults.get("host") or default_host
        email_default = defaults.get(CONF_CLOUD_EMAIL, "").strip()
        email_field = (
            vol.Optional(CONF_CLOUD_EMAIL, default=email_default)
            if email_default
            else vol.Optional(CONF_CLOUD_EMAIL)
        )
        return vol.Schema(
            {
                vol.Required("host", default=host_default): str,
                vol.Optional("port", default=defaults.get("port", DEFAULT_PORT)): int,
                vol.Required(
                    CONF_MODEL,
                    default=defaults.get(CONF_MODEL, MODEL_OPTIONS[0]),
                ): vol.In(
                    MODEL_OPTIONS
                ),
                email_field: str,
                vol.Optional(CONF_CLOUD_PASSWORD): PASSWORD_SELECTOR,
                vol.Optional(
                    CONF_CLOUD_REGION,
                    default=defaults.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION),
                ): vol.In(CLOUD_REGIONS),
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user enters IP, port, and model."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            model_label = user_input[CONF_MODEL]
            product_key = NARWAL_MODELS[model_label]
            self._pending_user_input = user_input
            self._pending_product_key = product_key
            cloud_email = user_input.get(CONF_CLOUD_EMAIL, "")
            cloud_password = user_input.get(CONF_CLOUD_PASSWORD, "")
            cloud_region = user_input.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION)
            if error := await _async_validate_cloud_credentials(
                self.hass,
                email=cloud_email,
                password=cloud_password,
                region=cloud_region,
            ):
                errors["base"] = error
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(host, user_input),
                    errors=errors,
                )

            if product_key in NO_BROADCAST_PRODUCT_KEYS:
                return await self.async_step_device_id()

            topic_prefix = None if product_key == "auto" else f"/{product_key}"
            client = NarwalClient(host=host, port=port, topic_prefix=topic_prefix)
            try:
                await client.connect()
            except Exception as ex:
                _LOGGER.warning(
                    "Setup connection failed: %s: %s", type(ex).__name__, ex
                )
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
                    return await self._async_create_entry(
                        client, device_info.device_id
                    )
            finally:
                await client.disconnect()

        # Keep whatever host we have: what the user just typed on a retry after
        # an error, otherwise the discovered address.
        default_host = (user_input or {}).get("host") or self._discovered_host
        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(default_host, user_input),
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
                _LOGGER.warning(
                    "Device ID validation failed: %s: %s", type(ex).__name__, ex
                )
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

    async def _async_create_entry(
        self, client: NarwalClient, device_id: str
    ) -> ConfigFlowResult:
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
                CONF_CLOUD_PRODUCT_ID: cloud_product_id_for_product_key(resolved_key),
                CONF_MODEL: model_label,
                **_cloud_data_from_input(setup),
            },
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
            previous_email = (
                options[CONF_CLOUD_EMAIL]
                if CONF_CLOUD_EMAIL in options
                else self.config_entry.data.get(CONF_CLOUD_EMAIL, "")
            )
            previous_password = (
                options[CONF_CLOUD_PASSWORD]
                if CONF_CLOUD_PASSWORD in options
                else self.config_entry.data.get(CONF_CLOUD_PASSWORD, "")
            )
            if (
                cloud_email
                and not cloud_password
                and (not previous_password or cloud_email != previous_email)
            ):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(
                        cloud_email,
                        cloud_region,
                    ),
                    errors={"base": "cloud_credentials_incomplete"},
                )
            validation_password = cloud_password or (
                previous_password if cloud_email == previous_email else ""
            )
            if cloud_email and (
                error := await _async_validate_cloud_credentials(
                    self.hass,
                    email=cloud_email,
                    password=validation_password,
                    region=cloud_region,
                )
            ):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(
                        cloud_email,
                        cloud_region,
                    ),
                    errors={"base": error},
                )
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
            return self.async_create_entry(title="", data=options)

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
            data_schema=self._options_schema(suggested_email, suggested_region),
        )

    @staticmethod
    def _options_schema(suggested_email: str, suggested_region: str) -> vol.Schema:
        """Return the cloud options schema."""
        return vol.Schema(
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
        )
