"""Narwal cloud API helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_CLOUD_REGION, narwal_cloud_hosts


class NarwalCloudError(Exception):
    """Raised when the Narwal cloud API fails."""


@dataclass(frozen=True)
class NarwalCloudConsumable:
    """Narwal cloud consumable item."""

    code: str
    name: str
    usage_duration: int
    total_duration: int
    item_type: int | None = None
    record_type: int | None = None
    consumable_type: int | None = None
    subtitle: str | None = None
    progress_bar: bool = False
    reset_supported: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> NarwalCloudConsumable:
        """Build a consumable item from the cloud payload."""
        return cls(
            code=str(data.get("consumables_code") or data.get("consumablesCode") or ""),
            name=str(data.get("name") or ""),
            usage_duration=_int_value(data.get("usage_duration") or data.get("usageDuration")),
            total_duration=_int_value(data.get("total_duration") or data.get("totalDuration")),
            item_type=_optional_int(data.get("item_type") or data.get("itemType")),
            record_type=_optional_int(data.get("record_type") or data.get("recordType")),
            consumable_type=_optional_int(data.get("type")),
            subtitle=data.get("subtitle"),
            progress_bar=_bool_flag(
                data.get("progress_bar_switch") or data.get("progressBarSwitch")
            ),
            reset_supported=_bool_flag(data.get("reset_btn_switch") or data.get("resetBtnSwitch")),
        )

    @property
    def has_life_counter(self) -> bool:
        """Return true when this consumable has a cloud progress counter."""
        return self.progress_bar and self.total_duration > 0

    @property
    def used_hours(self) -> float:
        """Return used duration in hours."""
        return round(self.usage_duration / 3600, 1)

    @property
    def total_hours(self) -> float:
        """Return expected lifetime in hours."""
        return round(self.total_duration / 3600, 1)

    @property
    def remaining_hours(self) -> float:
        """Return remaining lifetime in hours, clamped at zero."""
        return round(max(self.total_duration - self.usage_duration, 0) / 3600, 1)

    @property
    def used_percent(self) -> float:
        """Return used lifetime percentage."""
        if self.total_duration <= 0:
            return 0.0
        return round((self.usage_duration / self.total_duration) * 100, 1)

    @property
    def remaining_percent(self) -> float:
        """Return remaining lifetime percentage, clamped at zero."""
        return round(max(100.0 - self.used_percent, 0.0), 1)

    @property
    def is_overdue(self) -> bool:
        """Return true when the consumable is beyond its expected lifetime."""
        return self.total_duration > 0 and self.usage_duration >= self.total_duration


class NarwalCloudClient:
    """Small read-only Narwal cloud API client."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        email: str,
        password: str,
        region: str = DEFAULT_CLOUD_REGION,
    ) -> None:
        """Initialize the cloud API client."""
        self._session = async_get_clientsession(hass)
        self._email = email
        self._password = password
        auth_host, app_host = narwal_cloud_hosts(region)
        self._auth_host = auth_host.rstrip("/")
        self._app_host = app_host.rstrip("/")
        self._token: str | None = None

    async def async_get_consumables(
        self,
        *,
        device_id: str,
        product_id: str,
    ) -> list[NarwalCloudConsumable]:
        """Return cloud consumables for a device."""
        payload = {
            "deviceId": device_id,
            "productId": product_id,
        }
        data = await self._post_app_json(
            "/consumables-management-app-server/v3/consumables/list",
            payload,
        )
        if _is_token_error(data):
            await self.async_login(force=True)
            data = await self._post_app_json(
                "/consumables-management-app-server/v3/consumables/list",
                payload,
            )
        _raise_for_cloud_error(data, "consumables list")
        result = data.get("result")
        if not isinstance(result, list):
            raise NarwalCloudError("Narwal consumables list returned no result list")
        return [
            consumable
            for item in result
            if isinstance(item, dict)
            for consumable in [NarwalCloudConsumable.from_api(item)]
            if consumable.code and consumable.name
        ]

    async def async_login(self, *, force: bool = False) -> None:
        """Login and cache an Auth-Token value."""
        if self._token is not None and not force:
            return
        data = await self._post_json(
            f"{self._auth_host}/user-authentication-server/v2/login/loginByEmail",
            {
                "email": self._email,
                "password": self._password,
            },
            headers={},
        )
        _raise_for_cloud_error(data, "login")
        result = data.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("token"), str):
            raise NarwalCloudError("Narwal login returned no token")
        self._token = result["token"]

    async def _post_app_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to an app API endpoint."""
        await self.async_login()
        return await self._post_json(
            f"{self._app_host}{path}",
            payload,
            headers={"Auth-Token": self._token or ""},
        )

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """POST JSON and return a decoded object."""
        async with self._session.post(
            url,
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                **headers,
            },
            timeout=15,
        ) as response:
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise NarwalCloudError("Narwal cloud returned a non-object response")
        return data


def _int_value(value: Any) -> int:
    """Return an integer for loose cloud values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    """Return an optional integer for loose cloud values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_flag(value: Any) -> bool:
    """Return true for Narwal one/true flags."""
    return value is True or value == 1 or value == "1"


def _is_token_error(data: dict[str, Any]) -> bool:
    """Return true if the cloud payload says the token is invalid."""
    return data.get("err_code") in (130105, 130109) or data.get("msg") == "access token error"


def _raise_for_cloud_error(data: dict[str, Any], action: str) -> None:
    """Raise a redacted cloud error for failed API calls."""
    if (
        data.get("code") == 0
        or data.get("err_code") == 0
        or data.get("success") is True
    ):
        return
    message = data.get("msg") or "unknown error"
    code = data.get("err_code", data.get("code", "unknown"))
    raise NarwalCloudError(f"Narwal cloud {action} failed: {message} ({code})")
