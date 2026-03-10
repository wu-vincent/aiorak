"""Port of MaximumConnectTest.cpp — verify that max_connections is enforced."""

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


async def test_connect_up_to_max(server_factory, client_factory):
    """All clients connect successfully when count == max_connections."""
    max_conn = 4
    server = await server_factory(max_connections=max_conn)
    addr = server.local_address

    clients = []
    for _ in range(max_conn):
        clients.append(await client_factory(addr))

    await wait_for_peers(server, max_conn)
    assert len(server._peers) == max_conn


async def test_reject_beyond_max(server_factory, client_factory):
    """A client connecting beyond max_connections is rejected."""
    max_conn = 4
    server = await server_factory(max_connections=max_conn)
    addr = server.local_address

    # Fill all slots
    clients = []
    for _ in range(max_conn):
        clients.append(await client_factory(addr))

    await wait_for_peers(server, max_conn)
    assert len(server._peers) == max_conn

    # The 5th connection should fail — server sends rejection
    with pytest.raises(ConnectionRefusedError):
        await aiorak.connect(addr, timeout=2.0)

    # Server should still have exactly max_conn peers
    assert len(server._peers) == max_conn
