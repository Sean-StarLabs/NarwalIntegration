"""Tests for integration constants and capability helpers."""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_CLOUD_PRODUCT_ID,
    CONF_DOCK_LIGHT_SUPPORTED,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DOCK_LIGHT_PRODUCT_KEYS,
    NARWAL_MODELS,
    NO_BROADCAST_PRODUCT_KEYS,
    PRODUCT_KEY_ALIASES,
    cloud_product_id_for_product_key,
    configured_cloud_product_id,
    configured_model_name,
    is_dock_light_supported,
    model_label_for_product_key,
)
from narwal_client.const import KNOWN_PRODUCT_KEYS  # noqa: E402


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


def test_jx_is_a_selectable_model_and_broadcasts() -> None:
    """JX local control confirmed by @Smiorld (#42): selectable, broadcast-capable."""
    product_key = NARWAL_MODELS["Narwal JX"]
    assert product_key == "CGjuB6dzq7"
    assert product_key not in NO_BROADCAST_PRODUCT_KEYS
    assert configured_model_name(
        {CONF_MODEL: "Narwal JX", CONF_PRODUCT_KEY: product_key}
    ) == "JX"


def test_every_alias_names_a_real_selector_model() -> None:
    """An alias exists to reuse a selector label, so it must match one exactly.

    A typo here would silently store a model string no other code recognises --
    `configured_model_name` would hand the device registry a name that is not a
    model, and it would look like working behaviour.
    """
    for product_key, label in PRODUCT_KEY_ALIASES.items():
        assert label in NARWAL_MODELS, f"{product_key} names unknown model {label!r}"


def test_alias_keys_are_discoverable() -> None:
    """An alias the client never tries cannot be resolved in the first place.

    Auto-detect cycles KNOWN_PRODUCT_KEYS to provoke a response. A key that
    names a model but is absent from that list resolves only by luck, via the
    bare-topic frame or a broadcast.
    """
    for product_key in PRODUCT_KEY_ALIASES:
        assert product_key in KNOWN_PRODUCT_KEYS


def test_flow2_alternate_keys_resolve_to_flow_2() -> None:
    """All three known Flow 2 keys name the same model (#81).

    Only QxMSPG6VSO is reachable through the selector; iSuVlI1If2 and
    mkbqaprvrb are equally real and were previously unnamed.
    """
    for product_key in ("QxMSPG6VSO", "iSuVlI1If2", "mkbqaprvrb"):
        assert model_label_for_product_key(product_key) == "Narwal Flow 2"


def test_unknown_key_has_no_label() -> None:
    """An unrecognised key must not be guessed at -- the raw key is the honest answer."""
    assert model_label_for_product_key("zzzzUNKNOWN") is None
    assert model_label_for_product_key(None) is None


def test_dock_light_follows_every_flow2_key() -> None:
    """The dock light belongs to the model, not to one of its keys (#81).

    A hardcoded list meant @DeNo64's Flow 2, reporting an unlisted key, lost a
    feature its hardware has. Deriving the set keeps a new key from silently
    disabling it again.
    """
    assert DOCK_LIGHT_PRODUCT_KEYS == {"QxMSPG6VSO", "iSuVlI1If2", "mkbqaprvrb"}
    assert is_dock_light_supported({CONF_PRODUCT_KEY: "mkbqaprvrb"})
