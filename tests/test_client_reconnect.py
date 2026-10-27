"""Connection ownership regressions; every WebSocket is an offline fake."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import narwal_client.client as client_module
from narwal_client.client import NarwalClient, NarwalConnectionError


class FakeSocket:
    def __init__(self):
        self.sent = asyncio.Event()
        self.receiving = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.send = AsyncMock(side_effect=lambda frame: self.sent.set())
        self.close = AsyncMock(side_effect=lambda: self.incoming.put_nowait(None))
        self.ping = AsyncMock()

    async def recv(self):
        self.receiving.set()
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.recv()
        if message is None:
            raise StopAsyncIteration
        return message


@pytest.fixture(params=[False, True], ids=["cx7", "broadcasting"])
def client(monkeypatch, request):
    monkeypatch.setattr(
        client_module.websockets,
        "connect",
        AsyncMock(side_effect=AssertionError("Real connections are forbidden")),
    )
    instance = NarwalClient(
        "unused.invalid",
        device_id="test",
        topic_prefix="/hEA7OEshlx",
        supports_broadcasts=request.param,
    )
    instance._ws = FakeSocket()
    instance._connected.set()
    instance._listener_active = True
    return instance


async def cancel_tasks(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("listening", [True, False])
async def test_replacement_fails_pending_command_immediately(
    client, monkeypatch, listening
):
    client._listener_active = listening
    old = client._ws
    replacement = FakeSocket()
    monkeypatch.setattr(
        client_module.websockets, "connect", AsyncMock(return_value=replacement)
    )
    pending = asyncio.create_task(client.send_command("status/get_device_base_status"))
    try:
        async with asyncio.timeout(1):
            await old.sent.wait()
            if not listening:
                await old.receiving.wait()
            await client.connect()
            with pytest.raises(NarwalConnectionError):
                await pending
        assert client.connected
        assert not client._expected_responses
        replacement.close.assert_not_awaited()
    finally:
        await cancel_tasks(pending)


@pytest.mark.parametrize("listening", [True, False])
async def test_explicit_disconnect_wakes_request_before_socket_close_finishes(
    client, listening
):
    client._listener_active = listening
    old = client._ws
    close_started = asyncio.Event()
    finish_close = asyncio.Event()

    async def close():
        close_started.set()
        await finish_close.wait()

    old.close.side_effect = close
    pending = asyncio.create_task(client.send_command("status/get_device_base_status"))
    disconnect = None
    try:
        async with asyncio.timeout(1):
            await old.sent.wait()
            if not listening:
                await old.receiving.wait()
            disconnect = asyncio.create_task(client.disconnect())
            await close_started.wait()
            with pytest.raises(NarwalConnectionError):
                await pending
        assert not client.connected
        assert not client._expected_responses
        assert not disconnect.done()
    finally:
        finish_close.set()
        if disconnect is not None:
            await disconnect
        await cancel_tasks(pending)


async def test_listener_disconnect_releases_request_before_reconnect_backoff(client):
    old = client._ws
    listener = asyncio.create_task(client.start_listening())
    pending = asyncio.create_task(client.send_command("status/get_device_base_status"))
    try:
        async with asyncio.timeout(1):
            await old.sent.wait()
            await old.receiving.wait()
            client._should_reconnect = False
            old.incoming.put_nowait(None)
            await listener
            with pytest.raises(NarwalConnectionError):
                await pending
        assert not client.connected
        assert not client._expected_responses
    finally:
        await cancel_tasks(listener, pending)


async def test_old_listener_cleanup_preserves_replacement_state(client, monkeypatch):
    old = client._ws
    replacement = FakeSocket()
    monkeypatch.setattr(
        client_module.websockets, "connect", AsyncMock(return_value=replacement)
    )
    listener = asyncio.create_task(client.start_listening())
    try:
        async with asyncio.timeout(1):
            await old.receiving.wait()
            await client.connect()
            client._robot_awake = True
            await client._send_unawaited("common/notify_app_event", b"")
            expected = tuple(client._expected_responses)
            client._should_reconnect = False
            old.incoming.put_nowait(None)
            await listener
        assert client.connected
        assert client.robot_awake
        assert tuple(client._expected_responses) == expected
        replacement.close.assert_not_awaited()
    finally:
        await cancel_tasks(listener)


async def test_disconnect_during_send_consumes_failed_future(client):
    old = client._ws
    finish_send = asyncio.Event()

    async def send(frame):
        old.sent.set()
        await finish_send.wait()
        raise OSError("send failed after disconnect")

    old.send.side_effect = send
    pending = asyncio.create_task(client.send_command("task/pause"))
    try:
        async with asyncio.timeout(1):
            await old.sent.wait()
            future = client._expected_responses[0].future
            await client.disconnect()
            finish_send.set()
            with pytest.raises(OSError, match="send failed after disconnect"):
                await pending
        assert future.done()
        assert not client._expected_responses
        assert not future._log_traceback
    finally:
        finish_send.set()
        await cancel_tasks(pending)


async def test_queued_command_is_not_sent_after_disconnect(client):
    old = client._ws
    await client._command_lock.acquire()
    pending = asyncio.create_task(client.send_command("task/pause"))
    try:
        await asyncio.sleep(0)
        await client.disconnect()
        client._command_lock.release()
        with pytest.raises(NarwalConnectionError):
            await pending
        old.send.assert_not_awaited()
    finally:
        if client._command_lock.locked():
            client._command_lock.release()
        await cancel_tasks(pending)
