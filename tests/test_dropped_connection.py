"""Port of DroppedConnectionTest.cpp — verify that dropped connections are detected."""

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


def force_close_transport(target):
    """Close the underlying UDP transport without graceful disconnect."""
    target._socket._transport.close()


pytestmark = pytest.mark.asyncio


async def test_server_detects_client_gone(server_factory, client_factory):
    """Connect 5 clients, force-close 3, and verify the server detects the drops."""
    num_clients = 5
    num_to_drop = 3

    server = await server_factory(max_connections=num_clients)
    addr = server.local_address

    clients = []
    for _ in range(num_clients):
        clients.append(await client_factory(addr))

    await wait_for_peers(server, num_clients)
    assert len(server._peers) == num_clients

    # Force-close the first 3 clients (no graceful disconnect)
    for i in range(num_to_drop):
        force_close_transport(clients[i])

    # Wait for the server to detect the dropped connections.
    # Detection is timeout-based, so allow generous time (~15s).
    expected = num_clients - num_to_drop

    async def _wait_for_drop():
        while len(server._peers) > expected:
            await asyncio.sleep(0.2)

    await asyncio.wait_for(_wait_for_drop(), timeout=20.0)
    assert len(server._peers) == expected


async def test_client_detects_server_gone(server_factory, client_factory):
    """Connect a client, force-close the server, and verify the client detects it."""
    server = await server_factory()
    addr = server.local_address
    client = await client_factory(addr)

    await wait_for_peers(server, 1)

    # Force-close the server transport so the client gets no graceful disconnect
    force_close_transport(server)

    # The client's async iterator should eventually end when it detects
    # the server is gone.
    async def _drain_client():
        async for _data in client:
            pass  # pragma: no cover — we don't expect data, just disconnection

    await asyncio.wait_for(_drain_client(), timeout=20.0)
    assert not client.is_connected


async def test_random_disconnect_reconnect(server_factory, client_factory):
    """Randomly disconnect and reconnect clients for 5 seconds without crashing."""
    num_slots = 5
    server = await server_factory(max_connections=num_slots + 2)
    addr = server.local_address

    # Initial connections
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
            # Randomly either gracefully close or force-close
            if random.random() < 0.5:
                await cli.close()
            else:
                force_close_transport(cli)
            clients[idx] = None
        elif cli is None:
            # Reconnect
            try:
                new_cli = await aiorak.connect(addr, timeout=3.0)
                clients[idx] = new_cli
            except (asyncio.TimeoutError, OSError):
                pass  # Server may still be processing the drop

        await asyncio.sleep(random.uniform(0.05, 0.3))

    # Cleanup: close any remaining connected clients
    for cli in clients:
        if cli is not None:
            try:
                await cli.close()
            except Exception:
                pass

    # If we got here without an unhandled exception, the test passes.
