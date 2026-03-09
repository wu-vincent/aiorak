"""Integration tests adapted from PeerConnectDisconnectTest.cpp — connect/disconnect cycles."""

from __future__ import annotations

import asyncio

import pytest

import aiorak
from aiorak import EventType

from .conftest import collect_events


class TestConnectDisconnect:
    async def test_connect_disconnect_reconnect(self, server_factory):
        """Client connects, disconnects, reconnects to same server."""
        server = await server_factory()
        addr = server.local_address

        # First connection
        client1 = await aiorak.connect(addr, timeout=5.0)
        assert client1.is_connected
        events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        assert len(events) == 1

        # Disconnect
        await client1.close()
        disc_events = await collect_events(server, EventType.DISCONNECT, count=1, timeout=5.0)
        assert len(disc_events) == 1

        # Reconnect with a new client
        client2 = await aiorak.connect(addr, timeout=5.0)
        assert client2.is_connected
        events2 = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        assert len(events2) == 1

        await client2.close()

    async def test_multiple_clients_disconnect(self, server_factory, client_factory):
        """5 clients connect, some disconnect, verify server tracks correctly."""
        server = await server_factory(max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        # Wait for all connections
        connect_events = await collect_events(server, EventType.CONNECT, count=5, timeout=5.0)
        assert len(connect_events) == 5

        # Disconnect clients 0 and 2
        await clients[0].close()
        await clients[2].close()

        # Server should see 2 disconnects
        disc_events = await collect_events(server, EventType.DISCONNECT, count=2, timeout=5.0)
        assert len(disc_events) == 2

        # Remaining clients (1, 3, 4) should still work
        for i in [1, 3, 4]:
            assert clients[i].is_connected
