"""Integration tests — connect/disconnect cycles."""

import asyncio

import pytest

import aiorak
from aiorak import Connection

from .conftest import wait_for_peers


class TestConnectDisconnect:
    async def test_connect_disconnect_reconnect(self, server_factory):
        """Client connects, disconnects, reconnects to same server."""
        handler_calls = []
        handler_done = asyncio.Event()

        async def handler(conn: Connection):
            handler_calls.append(conn.address)
            async for data in conn:
                pass
            handler_done.set()

        server = await server_factory(handler=handler)
        addr = server.local_address

        # First connection
        client1 = await aiorak.connect(addr, timeout=5.0)
        assert client1.is_connected
        await wait_for_peers(server, 1, timeout=3.0)
        assert len(handler_calls) == 1

        # Disconnect
        await client1.close()
        await asyncio.wait_for(handler_done.wait(), timeout=5.0)

        handler_done.clear()

        # Reconnect with a new client
        client2 = await aiorak.connect(addr, timeout=5.0)
        assert client2.is_connected
        await wait_for_peers(server, 1, timeout=3.0)
        assert len(handler_calls) == 2

        await client2.close()

    async def test_multiple_clients_disconnect(self, server_factory, client_factory):
        """5 clients connect, some disconnect, verify server tracks correctly."""
        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 5, timeout=5.0)

        # Disconnect clients 0 and 2
        await clients[0].close()
        await clients[2].close()

        # Wait for server to detect disconnects
        await asyncio.sleep(0.5)

        # Remaining clients (1, 3, 4) should still work
        for i in [1, 3, 4]:
            assert clients[i].is_connected
