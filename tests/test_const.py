"""Tests for integration constants and capability helpers."""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_DOCK_LIGHT_SUPPORTED,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    is_dock_light_supported,
)


def test_dock_light_supported_for_flow_2_product_keys() -> None:
    assert is_dock_light_supported({CONF_PRODUCT_KEY: "QxMSPG6VSO"})
    assert is_dock_light_supported({CONF_PRODUCT_KEY: "iSuVlI1If2"})


def test_dock_light_hidden_for_flow_1_by_default() -> None:
    assert not is_dock_light_supported({CONF_PRODUCT_KEY: "QoEsI5qYXO", CONF_MODEL: "Narwal Flow"})


def test_dock_light_ignores_stale_model_label() -> None:
    assert not is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QoEsI5qYXO", CONF_MODEL: "Narwal Flow 2"}
    )


def test_dock_light_option_override() -> None:
    assert is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QoEsI5qYXO"},
        {CONF_DOCK_LIGHT_SUPPORTED: True},
    )
    assert not is_dock_light_supported(
        {CONF_PRODUCT_KEY: "QxMSPG6VSO"},
        {CONF_DOCK_LIGHT_SUPPORTED: False},
    )
