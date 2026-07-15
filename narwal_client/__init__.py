"""Narwal robot vacuum client library — local WebSocket API."""

from .client import NarwalClient, NarwalCommandError, NarwalConnectionError
from .const import (
    AmbientLightCtrlType,
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
    WorkingStatus,
)
from .models import (
    MAINTENANCE_BASE_STATION_CLEANING_FILTER,
    MAINTENANCE_BASE_STATION_CLEANING_FILTER_COMPONENT,
    MAINTENANCE_COMPONENT_IDS,
    CommandResponse,
    DeviceInfo,
    MapData,
    MapDisplayData,
    NarwalState,
    RoomInfo,
)
from .protocol import build_frame, parse_frame

__all__ = [
    "NarwalClient",
    "NarwalCommandError",
    "NarwalConnectionError",
    "NarwalState",
    "MAINTENANCE_BASE_STATION_CLEANING_FILTER",
    "MAINTENANCE_BASE_STATION_CLEANING_FILTER_COMPONENT",
    "MAINTENANCE_COMPONENT_IDS",
    "CommandResponse",
    "AmbientLightCtrlType",
    "CommandResult",
    "CleaningRoute",
    "DeviceInfo",
    "FanLevel",
    "MapData",
    "MapDisplayData",
    "MopHumidity",
    "MopStrengthLevel",
    "RoomInfo",
    "WorkMode",
    "WorkingStatus",
    "build_frame",
    "parse_frame",
]
