"""Integration tests for the connection handshake sequence."""


import asyncio

import pytest

import aiorak
from aiorak import EventType

from .conftest import collect_events


class TestHandshake:
    async def test_full_handshake_completes(self, server_factory, client_factory):
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        assert client.is_connected

    async def test_handshake_events(self, server_factory, client_factory):
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)

        # Server should get a CONNECT event
        events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        assert len(events) == 1
        assert events[0].type == EventType.CONNECT

    async def test_disconnect_graceful(self, server_factory, client_factory):
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)

        # Wait for server to see the connection
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Disconnect the client
        await client.close()

        # Server should get a DISCONNECT event
        events = await collect_events(server, EventType.DISCONNECT, count=1, timeout=5.0)
        assert len(events) == 1
        assert events[0].type == EventType.DISCONNECT


class TestTimeout:
    async def test_connect_timeout(self):
        """Connecting to a non-listening address should time out."""
        with pytest.raises(asyncio.TimeoutError):
            # Use a port that's unlikely to have a RakNet server
            await aiorak.connect(("127.0.0.1", 1), timeout=1.0)
