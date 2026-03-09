"""Concurrent client tests — adapted from ManyClientsOneServerBlockingTest.cpp.

Tests 20-32 clients connecting simultaneously, exchanging data, and
verifying server handles the load correctly.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _client_payload(client_idx: int, msg_idx: int) -> bytes:
    """Create a payload identifying client and message index."""
    return b"\x86" + struct.pack(">HH", client_idx, msg_idx)


class TestConcurrentClients:
    async def test_32_clients_connect(self, server_factory, client_factory):
        """All 32 clients get CONNECT events."""
        server = await server_factory(max_connections=32)
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        events = await collect_events(server, EventType.CONNECT, count=32, timeout=15.0)
        assert len(events) == 32

    async def test_32_clients_send_and_receive(self, server_factory, client_factory):
        """Each sends 1 msg, server echoes, all receive reply."""
        server = await server_factory(max_connections=32)
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        connect_events = await collect_events(
            server, EventType.CONNECT, count=32, timeout=15.0
        )

        # Each client sends one message
        for i, cli in enumerate(clients):
            await cli.send(_client_payload(i, 0))

        # Server receives all messages and echoes back
        recv_events = await collect_events(
            server, EventType.RECEIVE, count=32, timeout=10.0
        )
        assert len(recv_events) == 32

        # Echo back to each sender
        for e in recv_events:
            await server.send(e.address, e.data)

        # Each client should receive the echo
        for cli in clients:
            events = await collect_events(cli, EventType.RECEIVE, count=1, timeout=5.0)
            assert len(events) == 1

    async def test_broadcast_to_32_clients(self, server_factory, client_factory):
        """Server sends to all, all receive."""
        server = await server_factory(max_connections=32)
        addr = server.local_address

        clients = []
        for _ in range(32):
            cli = await client_factory(addr)
            clients.append(cli)

        connect_events = await collect_events(
            server, EventType.CONNECT, count=32, timeout=15.0
        )

        broadcast_msg = b"\x86broadcast"
        for e in connect_events:
            await server.send(e.address, broadcast_msg)

        # All clients should receive the broadcast
        tasks = [
            collect_events(cli, EventType.RECEIVE, count=1, timeout=5.0)
            for cli in clients
        ]
        results = await asyncio.gather(*tasks)
        for r in results:
            assert len(r) == 1
            assert r[0].data == broadcast_msg


class TestScaledDataExchange:
    async def test_20_clients_10_messages_each(self, server_factory, client_factory):
        """200 msgs total, per-sender ordering preserved."""
        server = await server_factory(max_connections=20)
        addr = server.local_address

        clients = []
        for _ in range(20):
            cli = await client_factory(addr)
            clients.append(cli)

        await collect_events(server, EventType.CONNECT, count=20, timeout=15.0)

        # Each client sends 10 messages
        for i, cli in enumerate(clients):
            for j in range(10):
                await cli.send(
                    _client_payload(i, j), reliability=Reliability.RELIABLE_ORDERED
                )

        total = 200
        events = await collect_events(server, EventType.RECEIVE, count=total, timeout=20.0)
        assert len(events) == total

        # Verify per-sender ordering
        per_sender: dict[int, list[int]] = {}
        for e in events:
            client_idx, msg_idx = struct.unpack(">HH", e.data[1:5])
            per_sender.setdefault(client_idx, []).append(msg_idx)

        for sender, indices in per_sender.items():
            assert indices == sorted(indices), (
                f"Sender {sender} out of order: {indices}"
            )

    async def test_server_sends_unique_to_each(self, server_factory, client_factory):
        """20 clients, each gets a different payload."""
        server = await server_factory(max_connections=20)
        addr = server.local_address

        clients = []
        for _ in range(20):
            cli = await client_factory(addr)
            clients.append(cli)

        connect_events = await collect_events(
            server, EventType.CONNECT, count=20, timeout=15.0
        )

        # Send unique message to each client
        for i, e in enumerate(connect_events):
            payload = b"\x86unique" + struct.pack(">H", i)
            await server.send(e.address, payload)

        # Each client should receive their unique message
        tasks = [
            collect_events(cli, EventType.RECEIVE, count=1, timeout=5.0)
            for cli in clients
        ]
        results = await asyncio.gather(*tasks)

        received_data = {r[0].data for r in results}
        # All 20 should be unique
        assert len(received_data) == 20
