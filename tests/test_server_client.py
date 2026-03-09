"""Integration tests adapted from ServerClientTest.cpp — multi-client connect + data flow."""

from __future__ import annotations

import asyncio

import pytest

import aiorak
from aiorak import EventType

from .conftest import collect_events


class TestMultipleClients:
    async def test_multiple_clients_connect(self, server_factory, client_factory):
        """10 clients all connect to one server, verify server sees all CONNECT events."""
        server = await server_factory(max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(10):
            cli = await client_factory(addr)
            clients.append(cli)

        events = await collect_events(server, EventType.CONNECT, count=10, timeout=10.0)
        assert len(events) == 10

    async def test_bidirectional_data_flow(self, server_factory, client_factory):
        """Server sends to all clients, each client sends to server."""
        server = await server_factory(max_connections=5)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        # Wait for all connections on the server side
        connect_events = await collect_events(server, EventType.CONNECT, count=5, timeout=5.0)
        client_addrs = [e.address for e in connect_events]

        # Each client sends a message to the server
        for i, cli in enumerate(clients):
            await cli.send(b"\x86client" + i.to_bytes(1, "big"))

        # Server should receive all messages
        server_msgs = await collect_events(server, EventType.RECEIVE, count=5, timeout=5.0)
        assert len(server_msgs) == 5

        # Server sends a reply to each client
        for addr_i in client_addrs:
            await server.send(addr_i, b"\x86reply")

        # Each client should receive a reply
        for cli in clients:
            events = await collect_events(cli, EventType.RECEIVE, count=1, timeout=5.0)
            assert len(events) == 1
            assert events[0].data == b"\x86reply"

    async def test_client_send_receive_round_trip(self, server_and_client):
        """Client sends, server echoes, client receives reply."""
        server, client, addr = server_and_client

        # Wait for server to see the connection
        srv_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        client_addr = srv_events[0].address

        # Client sends
        await client.send(b"\x86ping")

        # Server receives
        recv_events = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
        assert recv_events[0].data == b"\x86ping"

        # Server echoes back
        await server.send(client_addr, b"\x86pong")

        # Client receives
        client_recv = await collect_events(client, EventType.RECEIVE, count=1, timeout=5.0)
        assert client_recv[0].data == b"\x86pong"
