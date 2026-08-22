"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import (
    AmbientLightCtrlType,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)

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
    # AX26 ships under two marketing names on identical firmware (v01.02.00.15):
    # "Z10 Turbo" (@romedtino, #40) and "Z10 Pro" (@shin906710, #70), same product_key.
    "Narwal Freo Z10 Pro / Turbo": "qV6BujoYLz",
    "Narwal Freo X10 Pro": "CNbforyZWI",
    "Narwal Freo Z Ultra (CX7)": "hEA7OEshlx",
    "Other / Auto-detect": "auto",
}

CONF_DEVICE_ID = "device_id"
CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"
CONF_CLOUD_PRODUCT_ID = "cloud_product_id"
CONF_CLOUD_EMAIL = "cloud_email"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CLOUD_REGION = "cloud_region"

DEFAULT_CLOUD_REGION = "eu"
CLOUD_REGIONS = ("eu", "de", "us", "cn", "au", "jp", "kr", "sg")
CLOUD_CONSUMABLES_POLL_HOURS = 6

CLOUD_PRODUCT_IDS_BY_PRODUCT_KEY: dict[str, str] = {
    # CX7 uses this key for the local WebSocket topic but J5 in Narwal app APIs.
    "hEA7OEshlx": "J5",
}


def narwal_cloud_hosts(region: str) -> tuple[str, str]:
    """Return Narwal authentication and app hosts for a cloud region."""
    if region not in CLOUD_REGIONS:
        raise ValueError(f"Unsupported Narwal cloud region: {region}")
    return (
        f"https://{region}-idass.narwaltech.com",
        f"https://{region}-app.narwaltech.com",
    )


def cloud_product_id_for_product_key(product_key: str) -> str:
    """Return the Narwal app product id for a local WebSocket product key."""
    return CLOUD_PRODUCT_IDS_BY_PRODUCT_KEY.get(product_key, product_key)


def configured_cloud_product_id(data: dict) -> str:
    """Return the stored cloud product id, falling back for migrated entries."""
    product_key = str(data.get(CONF_PRODUCT_KEY, ""))
    return str(
        data.get(CONF_CLOUD_PRODUCT_ID)
        or cloud_product_id_for_product_key(product_key)
    )


NO_BROADCAST_PRODUCT_KEYS = {"hEA7OEshlx"}


def configured_model_name(data: dict) -> str:
    """Return device-registry model metadata for a config entry."""
    model = data.get(CONF_MODEL)
    if not model or model == "Narwal Flow":
        return MODEL
    if model == "Other / Auto-detect":
        product_key = data.get(CONF_PRODUCT_KEY)
        return f"Unknown ({product_key})" if product_key else "Unknown"
    return model.removeprefix("Narwal ")

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.LIGHT,
]

CONF_SHOW_ROOM_LABELS = "show_room_labels"
CONF_SHOW_FURNITURE = "show_furniture"
CONF_SHOW_FURNITURE_LABELS = "show_furniture_labels"

MAP_OPTION_DEFAULTS: dict[str, bool] = {
    CONF_SHOW_ROOM_LABELS: True,
    CONF_SHOW_FURNITURE: False,
    CONF_SHOW_FURNITURE_LABELS: False,
}

# Map view options read from the config entry's options by camera.py and applied by
# map_renderer.render_* . There is no options flow to set them yet, so they stay at
# their defaults — an unrotated, unzoomed map, i.e. the pre-#62 rendering.
CONF_MAP_ROTATION = "map_rotation"
CONF_MAP_ZOOM = "map_zoom"

MAP_ROTATION_DEFAULT = 0  # degrees clockwise; renderer accepts 0/90/180/270
MAP_ZOOM_DEFAULT = 1.0  # renderer clamps to 1.0–2.0

CONF_DOCK_LIGHT_SUPPORTED = "dock_light_supported"
SERVICE_CLEAN_ROOMS = "clean_rooms"

DOCK_LIGHT_PRODUCT_KEYS = {"QxMSPG6VSO", "iSuVlI1If2"}


def is_dock_light_supported(data: dict, options: dict | None = None) -> bool:
    """Return whether this configured model exposes dock ambient lighting."""
    if options and CONF_DOCK_LIGHT_SUPPORTED in options:
        return bool(options[CONF_DOCK_LIGHT_SUPPORTED])
    return data.get(CONF_PRODUCT_KEY) in DOCK_LIGHT_PRODUCT_KEYS


DOCK_LIGHT_MODES: dict[str, AmbientLightCtrlType] = {
    "Off": AmbientLightCtrlType.OFF,
    "Fireplace": AmbientLightCtrlType.WINTER_WARMTH,
    "Nightlight": AmbientLightCtrlType.NIGHT_LIGHT,
    "Purple": AmbientLightCtrlType.PURPLE_LIGHT,
}
DOCK_LIGHT_MODE_NAMES: dict[int, str] = {
    int(value): key for key, value in DOCK_LIGHT_MODES.items()
}

# HA fan_speed labels → FanLevel, from the app's user-visible suction names (sentence case, as HA shows fan_speed values directly). The enum members keep the app's internal identifiers, so DEEP surfaces as "Super" and SUPER as "Ultra".
_FAN_SPEED_CANONICAL: dict[str, FanLevel] = {
    "Quiet": FanLevel.MUTE,
    "Standard": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Super": FanLevel.DEEP,
    "Ultra": FanLevel.SUPER,
}

FAN_SPEED_LIST: list[str] = list(_FAN_SPEED_CANONICAL)

# Models whose app exposes only four suction tiers: FanLevel.SUPER (5) is unreachable
# there, and the live clean/set_fan_level enum (SweepFanLevel) has no SUPER at all, so
# offering "Ultra" would silently apply Strong. AX26 confirmed by app captures in #70 —
# its top tier ("Super powerful" / "super puissant") sends CleanParam tag 2 = 4 (DEEP).
NO_ULTRA_FAN_PRODUCT_KEYS: frozenset[str] = frozenset({"qV6BujoYLz"})


def fan_speed_list_for(data: dict) -> list[str]:
    """fan_speed options for this configured model — no "Ultra" where 5 is unreachable."""
    if data.get(CONF_PRODUCT_KEY) in NO_ULTRA_FAN_PRODUCT_KEYS:
        return [
            label
            for label, level in _FAN_SPEED_CANONICAL.items()
            if level is not FanLevel.SUPER
        ]
    return FAN_SPEED_LIST


# FAN_SPEED_MAP also accepts the "… powerful" labels shipped through v1.0.3 and the
# original lowercase fan_speed values (quiet/normal/strong/max) so existing automations
# keep working; these aliases are not offered in FAN_SPEED_LIST.
FAN_SPEED_MAP: dict[str, FanLevel] = _FAN_SPEED_CANONICAL | {
    "Super powerful": FanLevel.DEEP,
    "Ultra powerful": FanLevel.SUPER,
    "quiet": FanLevel.MUTE,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.SUPER,
}

# base_status field 15 (terminateReason) value → HA option key. From the decoded
# TaskResult enum (re/ENUMS.md). Live-confirmed: 1 = NORMAL_END.
TASK_RESULT_OPTIONS: dict[int, str] = {
    1: "normal_end",
    2: "user_force_end",
    3: "shutdown_force_end",
    4: "low_battery_force_end",
    5: "overflow_force_end",
    6: "pause_too_long_force_end",
    7: "system_error_force_end",
    8: "user_force_end_on_station",
    9: "user_force_end_on_app",
    10: "recall_end",
    11: "normal_end_replenish_map_fail",
    12: "map_unmatched_force_end",
    13: "relocation_fail_with_station_force_end",
    14: "relocation_fail_without_station_force_end",
    15: "pretask_error_force_end",
    16: "schedule_task_force_end",
    17: "unexecuted",
    18: "not_find_pet",
}

# Consumable alert enum value → name (ConsumableMaintainItem / ConsumableReplaceItem).
CONSUMABLE_MAINTAIN_ITEMS: dict[int, str] = {
    1: "dust box", 2: "dust filter", 4: "wash ribs", 6: "universal wheel",
    7: "cliff sensor", 8: "side distance sensor", 9: "water tank sponge",
    10: "anti-winding brush", 11: "smart module sponge", 20: "dust container",
}
CONSUMABLE_REPLACE_ITEMS: dict[int, str] = {
    1: "dust filter", 2: "mop", 3: "side brush", 4: "clear water filter",
    5: "roller brush", 6: "detergent", 7: "smart module filter", 8: "dust bag",
    20: "station bag", 21: "silver ions", 22: "curing agent", 23: "heavy detergent",
    24: "inner dust box",
}

# Best-effort help-center deep link for a robot error code. The app's goHelpCenterByCode
# builds <localized help base>?code=<n>&deviceId=…&lang=…; the exact base is a runtime
# i18n value we can't read, so this is inferred from the Flow's help-center family and
# should be corrected if a real error opens a different path. The raw code is the fallback.
ERROR_HELP_URL_TEMPLATE = (
    "https://help.narwal.com/helpcenter/vall/#/p2/question/all?eType=1&code={code}&lang=en-US"
)

# Select option id → robot enum. Option ids are rendered to the app's user-visible labels via translations.
WORK_MODE_MAP: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
WATER_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_MAP: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}

PASSES_MIN = 1
PASSES_MAX = 3
