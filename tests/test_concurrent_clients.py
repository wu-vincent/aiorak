"""Concurrent client tests — 20-32 clients connecting simultaneously."""

import asyncio
import struct

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import collect_packets, wait_for_peers


def _client_payload(client_idx: int, msg_idx: int) -> bytes:
    return b"\x86" + struct.pack(">HH", client_idx, msg_idx)


class TestConcurrentClients:
    async def test_32_clients_connect(self, server_factory, client_factory):
        """All 32 clients connect successfully."""
        connect_count = 0

        async def handler(conn: Connection):
            nonlocal connect_count
            connect_count += 1
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=32)
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 32, timeout=15.0)
        assert connect_count == 32

    async def test_32_clients_send_and_receive(self, server_factory, client_factory):
        """Each sends 1 msg, server echoes, all receive reply."""
        server = await server_factory(max_connections=32)  # default echo handler
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 32, timeout=15.0)

        for i, cli in enumerate(clients):
            await cli.send(_client_payload(i, 0))

        for cli in clients:
            packets = await collect_packets(cli, count=1, timeout=5.0)
            assert len(packets) == 1

    async def test_broadcast_to_32_clients(self, server_factory, client_factory):
        """Server sends to all, all receive."""
        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=32)
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 32, timeout=15.0)

        broadcast_msg = b"\x86broadcast"
        for peer in server._peers.values():
            await peer.send(broadcast_msg)

        tasks = [
            collect_packets(cli, count=1, timeout=5.0)
            for cli in clients
        ]
        results = await asyncio.gather(*tasks)
        for r in results:
            assert len(r) == 1
            assert r[0] == broadcast_msg


class TestScaledDataExchange:
    async def test_20_clients_10_messages_each(self, server_factory, client_factory):
        """200 msgs total, per-sender ordering preserved."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler, max_connections=20)
        addr = server.local_address

        clients = []
        for _ in range(20):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 20, timeout=15.0)

        for i, cli in enumerate(clients):
            for j in range(10):
                await cli.send(
                    _client_payload(i, j), reliability=Reliability.RELIABLE_ORDERED
                )

        total = 200
        items = []
        async def _collect():
            while len(items) < total:
                items.append(await received.get())

        await asyncio.wait_for(_collect(), timeout=20.0)
        assert len(items) == total

        per_sender: dict[int, list[int]] = {}
        for data in items:
            client_idx, msg_idx = struct.unpack(">HH", data[1:5])
            per_sender.setdefault(client_idx, []).append(msg_idx)

        for sender, indices in per_sender.items():
            assert indices == sorted(indices), (
                f"Sender {sender} out of order: {indices}"
            )

    async def test_server_sends_unique_to_each(self, server_factory, client_factory):
        """20 clients, each gets a different payload."""
        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=20)
        addr = server.local_address

        clients = []
        for _ in range(20):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 20, timeout=15.0)

        # Send unique message to each peer
        for i, peer in enumerate(server._peers.values()):
            payload = b"\x86unique" + struct.pack(">H", i)
            await peer.send(payload)

        tasks = [
            collect_packets(cli, count=1, timeout=5.0)
            for cli in clients
        ]
        results = await asyncio.gather(*tasks)

        received_data = {r[0] for r in results}
        assert len(received_data) == 20
