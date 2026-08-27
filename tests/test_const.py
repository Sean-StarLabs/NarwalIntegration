"""Tests for integration constants and capability helpers."""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_CLOUD_PRODUCT_ID,
    CONF_DOCK_LIGHT_SUPPORTED,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    NARWAL_MODELS,
    NO_BROADCAST_PRODUCT_KEYS,
    cloud_product_id_for_product_key,
    configured_cloud_product_id,
    configured_model_name,
    is_dock_light_supported,
)


def test_dock_light_supported_for_flow_2_product_keys() -> None:
    """Flow 2 product keys expose the dock light."""
    assert is_dock_light_supported({CONF_PRODUCT_KEY: "QxMSPG6VSO"})
    assert is_dock_light_supported({CONF_PRODUCT_KEY: "iSuVlI1If2"})


def test_dock_light_hidden_for_flow_1_by_default() -> None:
    """Flow 1 does not expose the Flow 2 dock light by label alone."""
    assert not is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QoEsI5qYXO", CONF_MODEL: "Narwal Flow"}
    )


def test_dock_light_ignores_stale_model_label() -> None:
    """The product key, not the possibly stale model label, controls support."""
    assert not is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QoEsI5qYXO", CONF_MODEL: "Narwal Flow 2"}
    )


def test_dock_light_option_override() -> None:
    """Config options can override the product-key support default."""
    assert is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QoEsI5qYXO"},
        {CONF_DOCK_LIGHT_SUPPORTED: True},
    )
    assert not is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QxMSPG6VSO"},
        {CONF_DOCK_LIGHT_SUPPORTED: False},
    )


def test_cx7_uses_j5_product_key_and_requires_addressed_setup() -> None:
    """CX7 uses the live-tested J5 key and cannot use broadcast discovery."""
    product_key = NARWAL_MODELS["Narwal Freo Z Ultra (CX7)"]
    assert product_key == "hEA7OEshlx"
    assert product_key in NO_BROADCAST_PRODUCT_KEYS


def test_cx7_cloud_product_id_differs_from_local_product_key() -> None:
    """Accessory cloud APIs use the app product identity, not the local topic key."""
    assert cloud_product_id_for_product_key("hEA7OEshlx") == "J5"
    assert configured_cloud_product_id({CONF_PRODUCT_KEY: "hEA7OEshlx"}) == "J5"
    assert configured_cloud_product_id(
        {
            CONF_PRODUCT_KEY: "hEA7OEshlx",
            CONF_CLOUD_PRODUCT_ID: "custom",
        }
    ) == "custom"


def test_configured_model_name_uses_selected_cx7_model() -> None:
    """Device metadata reflects the configured model instead of always showing Flow."""
    assert configured_model_name(
        {
            CONF_MODEL: "Narwal Freo Z Ultra (CX7)",
            CONF_PRODUCT_KEY: "hEA7OEshlx",
        }
    ) == "Freo Z Ultra (CX7)"


def test_configured_model_name_preserves_legacy_flow_name() -> None:
    """Existing Flow entries retain their hardware model identifier."""
    assert configured_model_name({CONF_MODEL: "Narwal Flow"}) == "Flow (AX12)"
