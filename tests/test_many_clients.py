"""Port of ManyClientsOneServer*Test.cpp — connect, disconnect, reconnect patterns.

Consolidates ManyClientsOneServerBlockingTest, ManyClientsOneServerNonBlockingTest,
ManyClientsOneServerDeallocateTest, and ManyClientsOneServerDeallocateBlockingTest.
"""

import asyncio
import sys
import time

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

NUM_CLIENTS = 32
CHURN_CLIENTS = 16


async def _noop_handler(conn):
    async for _ in conn:
        pass


async def _connect_all(addr, count, timeout=5.0):
    """Connect *count* clients to *addr* in parallel, return list of clients."""
    return list(await asyncio.gather(*(aiorak.connect(addr, timeout=timeout) for _ in range(count))))


async def _close_all(clients):
    """Gracefully close all clients, ignoring errors on already-closed ones."""
    await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)


@pytest.mark.timeout(15)
async def test_initial_connect(server_factory):
    """32 clients connect to a server and all are accepted."""
    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS + 10)
    addr = server.local_address

    clients = await _connect_all(addr, NUM_CLIENTS)
    try:
        await wait_for_peers(server, NUM_CLIENTS, timeout=10.0)
        assert len(server._peers) == NUM_CLIENTS
    finally:
        await _close_all(clients)


@pytest.mark.timeout(90)
async def test_disconnect_reconnect_cycle(server_factory):
    """32 clients connect, then go through 2 disconnect/reconnect cycles.

    Graceful disconnect sends ID_DISCONNECTION_NOTIFICATION so the server
    cleans up peers promptly (no need to wait for the 10 s timeout).
    """
    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS * 4)
    addr = server.local_address

    clients = await _connect_all(addr, NUM_CLIENTS)
    await wait_for_peers(server, NUM_CLIENTS, timeout=10.0)
    assert len(server._peers) >= NUM_CLIENTS

    for cycle in range(2):
        # Disconnect all clients — graceful disconnect flushes notification.
        await _close_all(clients)

        # Give the server a moment to process disconnect notifications.
        await asyncio.sleep(0.5)

        # Reconnect all clients (new instances required).
        clients = await _connect_all(addr, NUM_CLIENTS)
        await wait_for_peers(server, NUM_CLIENTS, timeout=10.0)
        assert len(server._peers) >= NUM_CLIENTS, (
            f"Cycle {cycle}: expected >= {NUM_CLIENTS} peers, got {len(server._peers)}"
        )

    await _close_all(clients)


def _disable_udp_connreset(server):
    """Prevent ICMP Port Unreachable errors from killing the server socket.

    On Windows, sending UDP to a closed port causes ``WSAECONNRESET`` which
    poisons the recv path.  ``SIO_UDP_CONNRESET`` disables this behaviour.
    """
    if sys.platform != "win32":
        return
    import ctypes

    sock = server._socket._transport.get_extra_info("socket")
    if sock is None:
        return
    SIO_UDP_CONNRESET = 0x9800000C
    ret = ctypes.c_ulong(0)
    ctypes.windll.ws2_32.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(ctypes.c_bool(False)),
        ctypes.sizeof(ctypes.c_bool),
        None,
        0,
        ctypes.byref(ret),
        None,
        None,
    )


@pytest.mark.timeout(30)
async def test_abrupt_disconnect(server_factory):
    """32 clients connect then have their transports forcefully closed.

    The server should detect the timeouts, then all clients reconnect.
    """
    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS * 4)
    _disable_udp_connreset(server)
    addr = server.local_address

    clients = await _connect_all(addr, NUM_CLIENTS)
    await wait_for_peers(server, NUM_CLIENTS, timeout=10.0)
    assert len(server._peers) == NUM_CLIENTS

    # Forcefully close all client transports.
    for c in clients:
        c._socket._transport.close()

    # Wait for the server to detect the dropped connections.
    # Must wait for both _peers and _connections to drain — stale entries
    # in _connections block new connection acceptance.
    deadline = time.monotonic() + 15.0
    while (len(server._peers) > 0 or len(server._connections) > 0) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    assert len(server._peers) == 0, f"Server still has {len(server._peers)} peers after abrupt disconnect"
    assert len(server._connections) == 0, (
        f"Server still has {len(server._connections)} connections after abrupt disconnect"
    )

    # Reconnect all clients.
    clients = await _connect_all(addr, NUM_CLIENTS)
    try:
        await wait_for_peers(server, NUM_CLIENTS, timeout=10.0)
        assert len(server._peers) == NUM_CLIENTS
    finally:
        await _close_all(clients)


@pytest.mark.timeout(15)
async def test_rapid_churn(server_factory):
    """16 clients rapidly close and reconnect for 3 seconds, then verify."""
    server = await server_factory(handler=_noop_handler, max_connections=CHURN_CLIENTS + 10)
    addr = server.local_address

    clients = await _connect_all(addr, CHURN_CLIENTS)
    await wait_for_peers(server, CHURN_CLIENTS, timeout=10.0)

    end_time = time.monotonic() + 3.0
    while time.monotonic() < end_time:
        # Close all, then reconnect.
        await _close_all(clients)
        await asyncio.sleep(0.05)
        clients = await _connect_all(addr, CHURN_CLIENTS, timeout=5.0)
        await asyncio.sleep(0.05)

    # Final close and clean reconnect to verify server is healthy.
    await _close_all(clients)

    # Give the server a moment to clean up stale peers.
    deadline = time.monotonic() + 5.0
    while len(server._peers) > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    clients = await _connect_all(addr, CHURN_CLIENTS)
    try:
        await wait_for_peers(server, CHURN_CLIENTS, timeout=10.0)
        assert len(server._peers) == CHURN_CLIENTS
    finally:
        await _close_all(clients)
