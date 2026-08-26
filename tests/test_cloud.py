"""Tests for Narwal cloud consumable payload handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.binary_sensor import (  # noqa: E402
    NarwalConsumableOverdueBinarySensor,
)
from custom_components.narwal.button import NarwalConsumableResetButton  # noqa: E402
from custom_components.narwal.cloud import (  # noqa: E402
    NarwalCloudClient,
    NarwalCloudConsumable,
    NarwalCloudError,
)
from custom_components.narwal.const import DOMAIN  # noqa: E402
from custom_components.narwal.sensor import (  # noqa: E402
    NarwalConsumableLifeSensor,
    NarwalConsumableUsedSensor,
)


class _Response:
    """Minimal async response context manager for cloud tests."""

    def __init__(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
        return self._payload


class _Session:
    """Record POST calls and return queued JSON payloads."""

    def __init__(self, *payloads: dict[str, Any] | _Response) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> _Response:
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        payload = self._payloads.pop(0)
        if isinstance(payload, _Response):
            return payload
        return _Response(payload)


@pytest.mark.asyncio
async def test_cloud_consumable_list_posts_expected_payload_and_headers() -> None:
    """Consumable list uses the Narwal app endpoint and cached auth token."""
    session = _Session(
        {"code": 0, "result": {"token": "tok1"}},
        {
            "code": 0,
            "result": [
                {
                    "consumablesCode": "side_brush",
                    "name": "Side brush",
                    "usageDuration": 3600,
                    "totalDuration": 7200,
                    "progressBarSwitch": 1,
                }
            ],
        },
    )

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=session,
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="secret")

    consumables = await client.async_get_consumables(
        device_id="dev1",
        product_id="prod1",
    )

    assert [item.code for item in consumables] == ["side_brush"]
    assert session.calls[0]["url"] == (
        "https://eu-idass.narwaltech.com/user-authentication-server/v2/login/loginByEmail"
    )
    assert session.calls[0]["json"] == {
        "email": "owner@example.com",
        "password": "secret",
    }
    assert session.calls[1]["url"] == (
        "https://eu-app.narwaltech.com/consumables-management-app-server/v3/consumables/list"
    )
    assert session.calls[1]["json"] == {
        "deviceId": "dev1",
        "productId": "prod1",
    }
    assert session.calls[1]["headers"]["Auth-Token"] == "tok1"


@pytest.mark.asyncio
async def test_cloud_consumable_list_refreshes_expired_token() -> None:
    """A token error forces login and retries the original list request."""
    session = _Session(
        {"code": 0, "result": {"token": "old-token"}},
        {"err_code": 130105, "msg": "access token error"},
        {"code": 0, "result": {"token": "new-token"}},
        {"code": 0, "result": []},
    )

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=session,
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")

    consumables = await client.async_get_consumables(
        device_id="dev1",
        product_id="prod1",
    )

    assert consumables == []
    assert [call["headers"].get("Auth-Token") for call in session.calls] == [
        None,
        "old-token",
        None,
        "new-token",
    ]
    assert (
        session.calls[1]["json"]
        == session.calls[3]["json"]
        == {
            "deviceId": "dev1",
            "productId": "prod1",
        }
    )


@pytest.mark.asyncio
async def test_cloud_consumable_list_refreshes_expired_token_from_code() -> None:
    """Some cloud endpoints report token errors in code rather than err_code."""
    session = _Session(
        {"code": 0, "result": {"token": "old-token"}},
        {"code": 130105, "msg": "access token error"},
        {"code": 0, "result": {"token": "new-token"}},
        {"code": 0, "result": []},
    )

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=session,
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")

    assert await client.async_get_consumables(device_id="dev1", product_id="prod1") == []
    assert [call["headers"].get("Auth-Token") for call in session.calls] == [
        None,
        "old-token",
        None,
        "new-token",
    ]


@pytest.mark.asyncio
async def test_cloud_consumable_reset_posts_expected_payload() -> None:
    """Consumable reset forwards all app-provided reset metadata."""
    session = _Session(
        {"code": 0, "result": {"token": "tok1"}},
        {"code": 0, "result": {}},
    )

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=session,
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")

    await client.async_reset_consumable(
        device_id="dev1",
        product_id="prod1",
        consumable_code="dock_filter",
        item_type=4,
        record_type=5,
        consumable_type=6,
    )

    assert session.calls[1]["url"] == (
        "https://eu-app.narwaltech.com/consumables-management-app-server/consumables/reset"
    )
    assert session.calls[1]["json"] == {
        "deviceId": "dev1",
        "productId": "prod1",
        "consumablesCode": "dock_filter",
        "type": 6,
        "itemType": 4,
        "recordType": 5,
    }
    assert session.calls[1]["headers"]["Auth-Token"] == "tok1"


def test_non_progress_consumable_can_still_report_overdue() -> None:
    """Detergent has no progress bar, but the app marks it overdue via usage >= total."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "detergent",
            "name": "Detergent",
            "progress_bar_switch": 0,
            "reset_btn_switch": 0,
            "usage_duration": 2,
            "total_duration": 1,
        }
    )

    assert not consumable.has_life_counter
    assert consumable.has_overdue_signal
    assert consumable.is_overdue


def test_non_progress_zero_duration_consumable_has_no_overdue_signal() -> None:
    """Static cloud tips should not create always-off overdue entities."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "caster",
            "name": "Caster",
            "progress_bar_switch": 0,
            "reset_btn_switch": 0,
            "usage_duration": 0,
            "total_duration": 0,
        }
    )

    assert not consumable.has_life_counter
    assert not consumable.has_overdue_signal
    assert not consumable.is_overdue


def test_usage_without_total_duration_has_no_overdue_signal() -> None:
    """A usage counter alone cannot produce a meaningful overdue binary sensor."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumables_code": "caster",
            "name": "Caster",
            "progress_bar_switch": 0,
            "reset_btn_switch": 0,
            "usage_duration": 3600,
            "total_duration": 0,
        }
    )

    assert not consumable.has_life_counter
    assert not consumable.has_overdue_signal
    assert not consumable.is_overdue


def test_cloud_consumable_preserves_zero_reset_metadata() -> None:
    """Zero-valued reset metadata is a valid cloud value, not missing data."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumablesCode": "dock_filter",
            "name": "Dock filter",
            "itemType": 0,
            "recordType": 0,
            "type": 0,
        }
    )

    assert consumable.item_type == 0
    assert consumable.record_type == 0
    assert consumable.consumable_type == 0


def test_cloud_consumable_preserves_zero_snake_case_values() -> None:
    """Snake-case zero values should not fall through to camel-case aliases."""
    consumable = NarwalCloudConsumable.from_api(
        {
            "consumablesCode": "side_brush",
            "name": "Side brush",
            "usage_duration": 0,
            "usageDuration": 3600,
            "total_duration": 0,
            "totalDuration": 7200,
            "progress_bar_switch": 0,
            "progressBarSwitch": 1,
            "reset_btn_switch": 0,
            "resetBtnSwitch": 1,
        }
    )

    assert consumable.usage_duration == 0
    assert consumable.total_duration == 0
    assert not consumable.progress_bar
    assert not consumable.reset_supported


@pytest.mark.asyncio
async def test_cloud_transport_failure_raises_cloud_error() -> None:
    """Transport failures should surface as controlled Narwal cloud errors."""

    class FailingSession:
        def post(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("offline")

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=FailingSession(),
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")
    client._token = "tok1"

    with pytest.raises(NarwalCloudError, match="request failed: RuntimeError"):
        await client.async_reset_consumable(
            device_id="dev1",
            product_id="prod1",
            consumable_code="dock_filter",
        )


@pytest.mark.asyncio
async def test_cloud_json_failure_raises_cloud_error() -> None:
    """JSON decode failures should not escape as raw aiohttp/value errors."""

    class BadJsonResponse:
        async def __aenter__(self) -> BadJsonResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
            raise ValueError("not json")

    class BadJsonSession:
        def post(self, *args: object, **kwargs: object) -> BadJsonResponse:
            return BadJsonResponse()

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=BadJsonSession(),
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")
    client._token = "tok1"

    with pytest.raises(NarwalCloudError, match="invalid JSON"):
        await client.async_reset_consumable(
            device_id="dev1",
            product_id="prod1",
            consumable_code="dock_filter",
        )


@pytest.mark.asyncio
async def test_cloud_http_failure_raises_cloud_error() -> None:
    """HTTP failures should not be decoded as successful cloud payloads."""
    session = _Session(
        {"code": 0, "result": {"token": "tok1"}},
        _Response({}, status=503),
    )

    with patch(
        "custom_components.narwal.cloud.async_get_clientsession",
        return_value=session,
    ):
        client = NarwalCloudClient(MagicMock(), email="owner@example.com", password="pw")

    with pytest.raises(NarwalCloudError, match="HTTP 503"):
        await client.async_reset_consumable(
            device_id="dev1",
            product_id="prod1",
            consumable_code="dock_filter",
        )


def test_cloud_consumable_entities_unavailable_when_refresh_failed() -> None:
    """Cloud-derived accessory entities should not expose stale data as current."""
    consumable = NarwalCloudConsumable(
        code="side_brush",
        name="Side brush",
        usage_duration=3600,
        total_duration=7200,
        progress_bar=True,
        reset_supported=True,
    )
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.cloud_consumables = {consumable.code: consumable}
    coordinator.cloud_consumables_error = None

    entities = (
        NarwalConsumableLifeSensor(coordinator, consumable),
        NarwalConsumableOverdueBinarySensor(coordinator, consumable),
        NarwalConsumableResetButton(coordinator, consumable),
    )

    assert all(entity.available for entity in entities)

    coordinator.last_update_success = False

    assert all(entity.available for entity in entities)

    coordinator.cloud_consumables_error = "cloud down"

    assert not any(entity.available for entity in entities)


def test_cloud_consumable_entities_unavailable_when_signal_disappears() -> None:
    """Existing entities should not fabricate values after a payload loses counters."""
    consumable = NarwalCloudConsumable(
        code="side_brush",
        name="Side brush",
        usage_duration=3600,
        total_duration=7200,
        progress_bar=True,
    )
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.cloud_consumables = {consumable.code: consumable}
    coordinator.cloud_consumables_error = None
    life = NarwalConsumableLifeSensor(coordinator, consumable)
    used = NarwalConsumableUsedSensor(coordinator, consumable)
    overdue = NarwalConsumableOverdueBinarySensor(coordinator, consumable)

    assert life.available
    assert used.available
    assert overdue.available

    coordinator.cloud_consumables = {
        consumable.code: NarwalCloudConsumable(
            code=consumable.code,
            name=consumable.name,
            usage_duration=0,
            total_duration=0,
            progress_bar=False,
        )
    }

    assert not life.available
    assert life.native_value is None
    assert not used.available
    assert used.native_value is None
    assert not overdue.available
    assert overdue.is_on is None


def test_cloud_consumable_device_uses_stable_dock_code() -> None:
    """Dock consumables should not depend on localized display names."""
    consumable = NarwalCloudConsumable(
        code="dock_filter",
        name="Filtre",
        usage_duration=3600,
        total_duration=7200,
        progress_bar=True,
    )
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"

    sensor = NarwalConsumableLifeSensor(coordinator, consumable)

    assert sensor._attr_device_info["identifiers"] == {(DOMAIN, "dev1_dock")}


@pytest.mark.asyncio
async def test_cloud_consumable_reset_rejects_unavailable_press() -> None:
    """Direct button services must not reset stale cloud consumable data."""
    consumable = NarwalCloudConsumable(
        code="side_brush",
        name="Side brush",
        usage_duration=3600,
        total_duration=7200,
        progress_bar=True,
        reset_supported=True,
    )
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    coordinator.cloud_consumables = {consumable.code: consumable}
    coordinator.cloud_consumables_error = "cloud down"
    coordinator.async_reset_cloud_consumable = MagicMock()
    button = NarwalConsumableResetButton(coordinator, consumable)

    with pytest.raises(Exception, match="not available"):
        await button.async_press()

    coordinator.async_reset_cloud_consumable.assert_not_called()
