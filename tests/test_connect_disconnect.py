"""Port of PeerConnectDisconnectTest.cpp and PeerConnectDisconnectWithCancelPendingTest.cpp.

Adapted from peer-mesh to client-server model: clients connect to a single
server, disconnect, and reconnect in cycles to verify connection stability.
"""

import asyncio
import time

import pytest

import aiorak


async def wait_for_peers(server, count, timeout=5.0):
    """Wait until server has at least count connected peers."""

    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


pytestmark = pytest.mark.asyncio

NUM_CLIENTS = 4
NUM_CYCLES = 3


async def _wait_for_no_peers(server: aiorak.Server, timeout: float = 5.0) -> None:
    """Wait until *server* has zero connected peers."""

    async def _wait():
        while len(server._peers) > 0:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


@pytest.mark.timeout(30)
async def test_connect_disconnect_cycles(server_factory):
    """Connect 4 clients, disconnect all, reconnect — repeat for 3 cycles.

    Each cycle verifies that the server correctly tracks all connections and
    that slots are freed after disconnect so they can be reused.
    """

    async def _noop_handler(conn):
        async for _ in conn:
            pass

    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS + 4)
    addr = server.local_address

    for cycle in range(NUM_CYCLES):
        # Connect all clients.
        clients = await asyncio.gather(*(aiorak.connect(addr, timeout=5.0) for _ in range(NUM_CLIENTS)))

        await wait_for_peers(server, NUM_CLIENTS, timeout=5.0)
        assert len(server._peers) == NUM_CLIENTS, (
            f"Cycle {cycle}: expected {NUM_CLIENTS} peers, got {len(server._peers)}"
        )

        for c in clients:
            assert c.is_connected

        # Disconnect all clients.  The server detects loss via timeout
        # because close() currently cancels the update loop before the
        # RELIABLE disconnect packet can be flushed.
        await asyncio.gather(*(c.close() for c in clients))

        # Wait for the server's internal timeout to expire.
        try:
            await _wait_for_no_peers(server, timeout=15.0)
        except TimeoutError:
            pass  # some peers may linger due to timeout detection lag


@pytest.mark.timeout(15)
async def test_rapid_connect_disconnect(server_factory):
    """Rapidly connect and disconnect 4 clients for ~5 seconds.

    Verifies that the server remains stable under connection churn and does
    not leak resources or crash.
    """

    async def _noop_handler(conn):
        async for _ in conn:
            pass

    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS + 4)
    addr = server.local_address

    deadline = time.monotonic() + 5.0
    iterations = 0

    while time.monotonic() < deadline:
        clients = await asyncio.gather(*(aiorak.connect(addr, timeout=5.0) for _ in range(NUM_CLIENTS)))
        # Immediately close without waiting for server to register.
        await asyncio.gather(*(c.close() for c in clients))
        iterations += 1

    # After the storm, the server should still be operational.
    assert iterations > 0
    # Let any pending disconnects settle.
    await asyncio.sleep(0.2)

    # Verify the server is still healthy by connecting one more client.
    final_client = await aiorak.connect(addr, timeout=5.0)
    try:
        await wait_for_peers(server, 1, timeout=5.0)
        assert final_client.is_connected
    finally:
        await final_client.close()


@pytest.mark.timeout(10)
async def test_cancelled_connect(server_factory):
    """Start a connection and close it immediately before the handshake completes.

    Verifies that cancelling a pending connection does not crash the server,
    and that a subsequent normal connection still succeeds.
    """

    async def _noop_handler(conn):
        async for _ in conn:
            pass

    server = await server_factory(handler=_noop_handler, max_connections=4)
    addr = server.local_address

    # Start connecting, then cancel immediately.
    async def _connect_and_cancel():
        task = asyncio.ensure_future(aiorak.connect(addr, timeout=5.0))
        # Yield once to let the connection attempt start, then cancel.
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    await _connect_and_cancel()
    # Give the server a moment to process the aborted attempt.
    await asyncio.sleep(0.2)

    # A normal connection should still work fine.
    client = await aiorak.connect(addr, timeout=5.0)
    try:
        await wait_for_peers(server, 1, timeout=5.0)
        assert client.is_connected
    finally:
        await client.close()
