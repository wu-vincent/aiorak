"""Port of DroppedConnectionConvertTest.cpp — verify that dropped connections are detected.

C++ test: RakNet/Samples/Tests/DroppedConnectionConvertTest.cpp
Uses CloseConnection(target, false) to silently drop connections, then
verifies the remote side detects the loss via timeout.
"""

import asyncio
import random

import pytest

import aiorak


async def wait_for_peers(server, count, timeout=5.0):
    """Wait until server has at least count connected peers."""

    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


pytestmark = pytest.mark.asyncio


async def test_server_detects_client_gone(server_factory, client_factory):
    """Connect 5 clients, silently close 3, and verify the server detects the drops.

    Mirrors C++ test case 0: CloseConnection(serverID, false, 0) on the client
    side — no notification is sent, so the server must detect the loss via timeout.
    """
    num_clients = 5
    num_to_drop = 3

    server = await server_factory(max_connections=num_clients)
    server.timeout = 2.0
    addr = server.local_address

    clients = []
    for _ in range(num_clients):
        clients.append(await client_factory(addr))

    await wait_for_peers(server, num_clients)
    assert len(server._peers) == num_clients

    # Silently close the first 3 clients (no notification — matches C++
    # CloseConnection(serverID, false, 0) from RakPeer.cpp:1650)
    for i in range(num_to_drop):
        await clients[i].close(notify=False)

    # Wait for the server to detect the dropped connections via timeout.
    expected = num_clients - num_to_drop

    async def _wait_for_drop():
        while len(server._peers) > expected:
            await asyncio.sleep(0.2)

    await asyncio.wait_for(_wait_for_drop(), timeout=10.0)
    assert len(server._peers) == expected


async def test_client_detects_server_gone(server_factory, client_factory):
    """Connect a client, silently close the server, and verify the client detects it.

    Mirrors the server-side equivalent of CloseConnection with no notification.
    """
    server = await server_factory()
    addr = server.local_address
    client = await client_factory(addr)
    client.timeout = 2.0

    await wait_for_peers(server, 1)

    # Silently shut down the server (no disconnect notification to clients)
    await server.close(notify=False)

    # The client's async iterator should end when it detects the server is gone.
    async def _drain_client():
        async for _data in client:
            pass  # pragma: no cover

    await asyncio.wait_for(_drain_client(), timeout=10.0)
    assert not client.is_connected


async def test_random_disconnect_reconnect(server_factory, client_factory):
    """Randomly disconnect and reconnect clients for 5 seconds without crashing.

    Mirrors C++ test case 2: randomly calls CloseConnection with either
    sendDisconnectionNotification=true or false, and reconnects.
    """
    num_slots = 5
    server = await server_factory(max_connections=num_slots + 2)
    addr = server.local_address

    clients: list[aiorak.Client | None] = []
    for _ in range(num_slots):
        cli = await client_factory(addr)
        clients.append(cli)

    await wait_for_peers(server, num_slots)

    deadline = asyncio.get_event_loop().time() + 5.0

    while asyncio.get_event_loop().time() < deadline:
        idx = random.randint(0, num_slots - 1)
        cli = clients[idx]

        if cli is not None and cli.is_connected:
            # Randomly either gracefully close or silently drop
            # (C++ test case 2: randomTest2 ? CloseConnection(false) : CloseConnection(true))
            if random.random() < 0.5:
                await cli.close(notify=True)
            else:
                await cli.close(notify=False)
            clients[idx] = None
        elif cli is None:
            # Reconnect
            try:
                new_cli = await aiorak.connect(addr, timeout=3.0)
                clients[idx] = new_cli
            except (asyncio.TimeoutError, OSError):
                pass  # Server may still be processing the drop

        await asyncio.sleep(random.uniform(0.05, 0.3))

    # Cleanup
    for cli in clients:
        if cli is not None:
            try:
                await cli.close()
            except Exception:
                pass
