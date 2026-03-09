"""Integration tests — multi-client connect + data flow."""

import asyncio

import pytest

import aiorak
from aiorak import Connection

from .conftest import collect_packets, wait_for_peers


class TestMultipleClients:
    async def test_multiple_clients_connect(self, server_factory, client_factory):
        """10 clients all connect to one server, verify server sees all peers."""
        connected = asyncio.Queue()

        async def handler(conn: Connection):
            connected.put_nowait(conn.address)
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(10):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 10, timeout=10.0)
        assert connected.qsize() == 10

    async def test_bidirectional_data_flow(self, server_factory, client_factory):
        """Server sends to all clients, each client sends to server."""
        received_on_server = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received_on_server.put_nowait(data)
                await conn.send(b"\x86reply")

        server = await server_factory(handler=handler, max_connections=5)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 5, timeout=5.0)

        # Each client sends a message to the server
        for i, cli in enumerate(clients):
            await cli.send(b"\x86client" + i.to_bytes(1, "big"))

        # Wait for server to receive all messages
        for _ in range(5):
            await asyncio.wait_for(received_on_server.get(), timeout=5.0)

        # Each client should receive a reply
        for cli in clients:
            packets = await collect_packets(cli, count=1, timeout=5.0)
            assert len(packets) == 1
            assert packets[0] == b"\x86reply"

    async def test_client_send_receive_round_trip(self, server_and_client):
        """Client sends, server echoes (default handler), client receives reply."""
        server, client, addr = server_and_client

        await client.send(b"\x86ping")

        packets = await collect_packets(client, count=1, timeout=5.0)
        assert packets[0] == b"\x86ping"
