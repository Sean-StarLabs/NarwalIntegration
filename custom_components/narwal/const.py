"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import AmbientLightCtrlType, FanLevel

DOMAIN = "narwal"
DEFAULT_PORT = 9002

MANUFACTURER = "Narwal"
MODEL = "Flow (AX12)"

# Model selector for config flow.
# Keys are user-facing labels; values are product key prefixes.
# "auto" cycles all known keys during discovery (slower, fallback).
NARWAL_MODELS: dict[str, str] = {
    "Narwal Flow": "QoEsI5qYXO",
    "Narwal Flow 2": "QxMSPG6VSO",
    "Narwal Freo Z10 Ultra": "DrzDKQ0MU8",
    "Narwal Freo X10 Pro": "CNbforyZWI",
    "Other / Auto-detect": "auto",
}

CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"
CONF_CLOUD_EMAIL = "cloud_email"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CLOUD_REGION = "cloud_region"

DEFAULT_CLOUD_REGION = "eu"
CLOUD_REGIONS = ("eu", "de", "us", "cn", "au", "jp", "kr", "sg")
CLOUD_CONSUMABLES_POLL_HOURS = 6


def narwal_cloud_hosts(region: str) -> tuple[str, str]:
    """Return Narwal authentication and app hosts for a cloud region."""
    if region not in CLOUD_REGIONS:
        raise ValueError(f"Unsupported Narwal cloud region: {region}")
    return (
        f"https://{region}-idass.narwaltech.com",
        f"https://{region}-app.narwaltech.com",
    )

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.LIGHT,
]

CONF_SHOW_ROOM_LABELS = "show_room_labels"
CONF_SHOW_FURNITURE = "show_furniture"
CONF_SHOW_FURNITURE_LABELS = "show_furniture_labels"
CONF_MAP_ROTATION = "map_rotation"
CONF_MAP_ZOOM = "map_zoom"
CONF_DOCK_LIGHT_SUPPORTED = "dock_light_supported"
SERVICE_CLEAN_ROOMS = "clean_rooms"
SERVICE_SET_DOCK_LIGHT = "set_dock_light"
SERVICE_SET_LED = "set_led"

FLOW_2_PRODUCT_KEYS = frozenset({"QxMSPG6VSO", "iSuVlI1If2"})
DOCK_LIGHT_PRODUCT_KEYS = FLOW_2_PRODUCT_KEYS


def is_dock_light_supported(data: dict, options: dict | None = None) -> bool:
    """Return whether this configured model exposes dock ambient lighting."""
    if options and CONF_DOCK_LIGHT_SUPPORTED in options:
        return bool(options[CONF_DOCK_LIGHT_SUPPORTED])
    return data.get(CONF_PRODUCT_KEY) in DOCK_LIGHT_PRODUCT_KEYS


def is_maintenance_alerts_supported(
    data: dict, discovered_product_key: str | None = None
) -> bool:
    """Return whether this configured model reports maintenance alerts."""
    return (
        data.get(CONF_PRODUCT_KEY) in FLOW_2_PRODUCT_KEYS
        or discovered_product_key in FLOW_2_PRODUCT_KEYS
    )


DOCK_LIGHT_MODES: dict[str, AmbientLightCtrlType] = {
    "Off": AmbientLightCtrlType.OFF,
    "Fireplace": AmbientLightCtrlType.WINTER_WARMTH,
    "Nightlight": AmbientLightCtrlType.NIGHT_LIGHT,
    "Purple": AmbientLightCtrlType.PURPLE_LIGHT,
}
DOCK_LIGHT_SERVICE_MODES: dict[str, AmbientLightCtrlType] = {
    key.lower(): value for key, value in DOCK_LIGHT_MODES.items()
}
DOCK_LIGHT_MODE_NAMES: dict[int, str] = {
    int(value): key for key, value in DOCK_LIGHT_MODES.items()
}

MAP_OPTION_DEFAULTS: dict[str, bool] = {
    CONF_SHOW_ROOM_LABELS: True,
    CONF_SHOW_FURNITURE: False,
    CONF_SHOW_FURNITURE_LABELS: False,
}

MAP_ROTATION_DEFAULT = 0
MAP_ZOOM_DEFAULT = 1.0

# HA fan_speed labels for the live clean/set_fan_level command. Its
# SweepFanLevel enum stops at DEEP; SUPER remains available to clean settings.
_FAN_SPEED_CANONICAL: dict[str, FanLevel] = {
    "Quiet": FanLevel.MUTE,
    "Standard": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Super powerful": FanLevel.DEEP,
}

FAN_SPEED_LIST: list[str] = list(_FAN_SPEED_CANONICAL)

# FAN_SPEED_MAP also accepts the original lowercase fan_speed values (quiet/normal/strong/max) so existing automations keep working; these aliases are not offered in FAN_SPEED_LIST.
FAN_SPEED_MAP: dict[str, FanLevel] = _FAN_SPEED_CANONICAL | {
    "quiet": FanLevel.MUTE,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.DEEP,
}
