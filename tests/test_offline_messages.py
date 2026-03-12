"""Port of OfflineMessagesTest.cpp - verify offline ping/pong with custom data."""

import asyncio

import pytest

import aiorak


async def wait_for_peers(server, count, timeout=5.0):
    """Wait until server has at least count connected peers."""

    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


pytestmark = pytest.mark.asyncio

CUSTOM_DATA = b"Hello from aiorak server!"


async def test_ping_receives_response(server_factory):
    """A ping to the server returns a valid PingResponse."""
    server = await server_factory()
    server.set_offline_ping_response(CUSTOM_DATA)
    addr = server.local_address

    response = await aiorak.ping(addr, timeout=3.0)
    assert response is not None
    assert response.latency_ms >= 0


async def test_ping_returns_custom_data(server_factory):
    """The ping response data matches the server's offline ping response."""
    server = await server_factory()
    server.set_offline_ping_response(CUSTOM_DATA)
    addr = server.local_address

    response = await aiorak.ping(addr, timeout=3.0)
    assert response.data == CUSTOM_DATA


async def test_ping_only_if_open_full_server(server_factory, client_factory):
    """Pinging a full server with only_if_open=True should timeout."""
    server = await server_factory(max_connections=1)
    server.set_offline_ping_response(CUSTOM_DATA)
    addr = server.local_address

    # Fill the single slot
    await client_factory(addr)
    await wait_for_peers(server, 1)

    with pytest.raises(TimeoutError):
        await aiorak.ping(addr, timeout=2.0, only_if_open=True)


async def test_ping_regardless_of_capacity(server_factory, client_factory):
    """Pinging a full server with only_if_open=False still returns a response."""
    server = await server_factory(max_connections=1)
    server.set_offline_ping_response(CUSTOM_DATA)
    addr = server.local_address

    # Fill the single slot
    await client_factory(addr)
    await wait_for_peers(server, 1)

    response = await aiorak.ping(addr, timeout=3.0, only_if_open=False)
    assert response is not None
    assert response.data == CUSTOM_DATA
