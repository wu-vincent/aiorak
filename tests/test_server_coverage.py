"""Tests targeting uncovered branches in _server.py."""

import asyncio

import pytest

import aiorak
from aiorak._connection import ConnectionState

pytestmark = pytest.mark.asyncio


async def _noop_handler(conn):
    async for _ in conn:
        pass


@pytest.mark.timeout(10)
async def test_server_context_manager(server_factory):
    """async with on Server calls close() on exit."""
    server = await server_factory(handler=_noop_handler)
    addr = server.address

    client = await aiorak.connect(addr, timeout=5.0)
    await asyncio.sleep(0.05)
    assert client.is_connected

    # Use __aenter__/__aexit__ explicitly (server already started by factory)
    async with server:
        # Server should be functional inside the context
        assert len(server._peers) >= 1

    # After exiting, server should be closed
    assert server._closed
    await client.close()


@pytest.mark.timeout(10)
async def test_broadcast_with_exclude(server_factory, client_factory):
    """broadcast(exclude=conn) sends to all peers except the excluded one."""
    server = await server_factory(handler=_noop_handler, max_connections=4)
    addr = server.address

    client1 = await client_factory(addr)
    client2 = await client_factory(addr)
    await asyncio.sleep(0.1)

    peers = server.connections
    assert len(peers) == 2

    # Broadcast excluding the first peer
    excluded = peers[0]
    server.broadcast(b"hello", exclude=excluded)
    await asyncio.sleep(0.2)

    # The non-excluded client should receive data; we verify at least one got it.
    # Since we don't know which client maps to which peer, just verify broadcast worked.
    got1 = []
    got2 = []

    async def try_recv(client, bucket):
        try:
            async for data in client:
                bucket.append(data)
                break
        except Exception:
            pass

    t1 = asyncio.create_task(asyncio.wait_for(try_recv(client1, got1), timeout=0.5))
    t2 = asyncio.create_task(asyncio.wait_for(try_recv(client2, got2), timeout=0.5))
    await asyncio.gather(t1, t2, return_exceptions=True)

    # Exactly one client should have received the broadcast
    total_received = len(got1) + len(got2)
    assert total_received == 1, f"Expected 1 client to receive broadcast, got {total_received}"


@pytest.mark.timeout(10)
async def test_disconnect_without_notify(server_factory, client_factory):
    """server.disconnect(conn, notify=False) drops silently."""
    server = await server_factory(handler=_noop_handler)
    addr = server.address

    await client_factory(addr)
    await asyncio.sleep(0.1)

    peers = server.connections
    assert len(peers) == 1

    server.disconnect(peers[0], notify=False)
    # The connection should transition to DISCONNECTED immediately
    assert peers[0].state == ConnectionState.DISCONNECTED


@pytest.mark.timeout(10)
async def test_disconnect_with_notify(server_factory, client_factory):
    """server.disconnect(conn, notify=True) sends notification."""
    server = await server_factory(handler=_noop_handler)
    addr = server.address

    await client_factory(addr)
    await asyncio.sleep(0.1)

    peers = server.connections
    assert len(peers) == 1

    server.disconnect(peers[0], notify=True)
    # Should transition to DISCONNECTING (notification queued)
    assert peers[0].state == ConnectionState.DISCONNECTING


@pytest.mark.timeout(10)
async def test_server_wide_timeout(server_factory, client_factory):
    """set_timeout()/get_timeout() with conn=None updates all connections."""
    server = await server_factory(handler=_noop_handler)
    addr = server.address

    await client_factory(addr)
    await asyncio.sleep(0.1)

    # Get default timeout
    default_timeout = server.get_timeout()
    assert isinstance(default_timeout, float)

    # Set server-wide timeout
    server.set_timeout(42.0)
    assert server.get_timeout() == 42.0

    # All active connections should have the new timeout
    for conn in server._connections.values():
        assert conn.timeout == 42.0

    # Per-connection timeout
    peers = server.connections
    assert len(peers) >= 1
    server.set_timeout(99.0, peers[0])
    assert server.get_timeout(peers[0]) == 99.0
    # Server default should be unchanged
    assert server.get_timeout() == 42.0


@pytest.mark.timeout(10)
async def test_get_mtu(server_factory, client_factory):
    """server.get_mtu(conn) returns the negotiated MTU."""
    server = await server_factory(handler=_noop_handler)
    addr = server.address

    await client_factory(addr)
    await asyncio.sleep(0.1)

    peers = server.connections
    assert len(peers) == 1

    mtu = server.get_mtu(peers[0])
    assert isinstance(mtu, int)
    assert mtu > 0


@pytest.mark.timeout(10)
async def test_broadcast_only_connected_peers(server_factory, client_factory):
    """broadcast() skips peers not in CONNECTED state."""
    server = await server_factory(handler=_noop_handler, max_connections=4)
    addr = server.address

    client1 = await client_factory(addr)
    client2 = await client_factory(addr)
    await asyncio.sleep(0.1)

    peers = server.connections
    assert len(peers) == 2

    # Force one peer into DISCONNECTING state
    peers[0].disconnect()
    assert peers[0].state == ConnectionState.DISCONNECTING

    # Broadcast should only reach the still-connected peer
    server.broadcast(b"test")
    await asyncio.sleep(0.2)

    # Verify at least one client gets the message
    got = []

    async def try_recv(client, bucket):
        try:
            async for data in client:
                bucket.append(data)
                break
        except Exception:
            pass

    # Only one client should receive
    t1 = asyncio.create_task(asyncio.wait_for(try_recv(client1, got), timeout=0.5))
    t2_got = []
    t2 = asyncio.create_task(asyncio.wait_for(try_recv(client2, t2_got), timeout=0.5))
    await asyncio.gather(t1, t2, return_exceptions=True)
    total = len(got) + len(t2_got)
    assert total == 1, f"Expected 1 client to receive broadcast, got {total}"


@pytest.mark.timeout(10)
async def test_server_repr(server_factory, client_factory):
    """Server.__repr__ includes address and connection count."""
    server = await server_factory(handler=_noop_handler)
    r = repr(server)
    assert "Server" in r
    assert "listening" in r
    assert "connections=" in r


@pytest.mark.timeout(10)
async def test_server_properties(server_factory):
    """Test server.guid, server.connections, server.timeout property."""
    server = await server_factory(handler=_noop_handler)
    assert isinstance(server.guid, int)
    assert server.connections == []

    # timeout property delegates to get/set_timeout
    original = server.timeout
    server.timeout = 30.0
    assert server.timeout == 30.0
    server.timeout = original
