"""Connection churn tests — adapted from ManyClientsOneServerNonBlockingTest.cpp,
ManyClientsOneServerDeallocateTest.cpp, and ComprehensiveTest.cpp.

Tests rapid connect/disconnect cycles, interleaved operations, and stress patterns.
"""

from __future__ import annotations

import asyncio
import random

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events, force_close_transport


class TestChurnCycles:
    async def test_connect_disconnect_10_cycles(self, server_factory):
        """10 sequential connect/exchange/disconnect cycles."""
        server = await server_factory()
        addr = server.local_address

        for cycle in range(10):
            client = await aiorak.connect(addr, timeout=5.0)
            assert client.is_connected

            await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

            # Exchange a message
            await client.send(b"\x86cycle" + bytes([cycle]))
            events = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
            assert len(events) == 1

            await client.close()
            await collect_events(server, EventType.DISCONNECT, count=1, timeout=5.0)

    async def test_32_clients_disconnect_reconnect(self, server_factory, client_factory):
        """Connect 32, disconnect all, reconnect 32 new ones."""
        server = await server_factory(max_connections=64)
        addr = server.local_address

        # First batch: 32 clients
        batch1 = []
        for _ in range(32):
            cli = await client_factory(addr)
            batch1.append(cli)

        await collect_events(server, EventType.CONNECT, count=32, timeout=15.0)

        # Disconnect all
        for cli in batch1:
            await cli.close()

        await collect_events(server, EventType.DISCONNECT, count=32, timeout=15.0)

        # Second batch: 32 new clients
        batch2 = []
        for _ in range(32):
            cli = await aiorak.connect(addr, timeout=5.0)
            batch2.append(cli)

        events = await collect_events(server, EventType.CONNECT, count=32, timeout=15.0)
        assert len(events) == 32

        # Clean up batch2
        for cli in batch2:
            await cli.close()


class TestInterleavedChurn:
    async def test_interleaved_connect_disconnect(self, server_factory, client_factory):
        """Connect 5, disconnect 0/2/4, connect 3 new, verify data flows."""
        server = await server_factory(max_connections=10)
        addr = server.local_address

        # Connect 5 clients
        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        await collect_events(server, EventType.CONNECT, count=5, timeout=10.0)

        # Disconnect clients 0, 2, 4
        for i in [0, 2, 4]:
            await clients[i].close()

        await collect_events(server, EventType.DISCONNECT, count=3, timeout=10.0)

        # Connect 3 new clients
        new_clients = []
        for _ in range(3):
            cli = await client_factory(addr)
            new_clients.append(cli)

        await collect_events(server, EventType.CONNECT, count=3, timeout=10.0)

        # Remaining original clients (1, 3) should still work
        for i in [1, 3]:
            await clients[i].send(b"\x86still_alive")

        events = await collect_events(server, EventType.RECEIVE, count=2, timeout=5.0)
        assert len(events) == 2

        # New clients should also work
        for cli in new_clients:
            await cli.send(b"\x86new_client")

        events = await collect_events(server, EventType.RECEIVE, count=3, timeout=5.0)
        assert len(events) == 3


class TestAbruptDisconnect:
    async def test_client_destroyed_without_close(
        self, server_factory, client_factory, monkeypatch
    ):
        """Cancel client tasks, server detects timeout disconnect."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 2.0)

        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Force-close the transport without graceful disconnect
        force_close_transport(client)

        events = await collect_events(
            server, EventType.DISCONNECT, count=1, timeout=5.0
        )
        assert len(events) == 1

    async def test_multiple_abrupt_disconnects(
        self, server_factory, client_factory, monkeypatch
    ):
        """5 clients force-closed, server sees 5 DISCONNECTs."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 2.0)

        server = await server_factory(max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        await collect_events(server, EventType.CONNECT, count=5, timeout=10.0)

        # Force-close all clients
        for cli in clients:
            force_close_transport(cli)

        events = await collect_events(
            server, EventType.DISCONNECT, count=5, timeout=5.0
        )
        assert len(events) == 5


class TestStress:
    async def test_random_operations(self, server_factory, monkeypatch):
        """5 client slots, 50 random ops, verify no unhandled exceptions."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 3.0)

        server = await server_factory(max_connections=10)
        addr = server.local_address

        slots: list[aiorak.Client | None] = [None] * 5
        rng = random.Random(42)  # Deterministic seed
        errors: list[Exception] = []

        for op_num in range(50):
            slot = rng.randint(0, 4)
            action = rng.choice(["connect", "disconnect", "send"])

            try:
                if action == "connect":
                    if slots[slot] is not None:
                        await slots[slot].close()
                    cli = await aiorak.connect(addr, timeout=3.0)
                    slots[slot] = cli

                elif action == "disconnect":
                    if slots[slot] is not None:
                        await slots[slot].close()
                        slots[slot] = None

                elif action == "send":
                    if slots[slot] is not None and slots[slot].is_connected:
                        await slots[slot].send(
                            b"\x86stress" + op_num.to_bytes(2, "big"),
                            reliability=Reliability.RELIABLE_ORDERED,
                        )

            except (RuntimeError, asyncio.TimeoutError, OSError):
                # Expected errors during churn
                pass
            except Exception as exc:
                errors.append(exc)

        # Clean up
        for cli in slots:
            if cli is not None:
                try:
                    await cli.close()
                except Exception:
                    pass

        assert len(errors) == 0, f"Unexpected errors: {errors}"
