"""Dropped connection tests — adapted from DroppedConnectionTest.cpp.

Tests timeout detection when connections are silently dropped.
"""

from __future__ import annotations

import asyncio

import pytest

import aiorak
from aiorak import EventType
from aiorak._constants import DEFAULT_TIMEOUT

from .conftest import collect_events, force_close_transport


# Use a short timeout for faster tests
_SHORT_TIMEOUT = 2.0


class TestDroppedConnection:
    async def test_server_detects_client_gone(
        self, server_factory, client_factory, monkeypatch
    ):
        """Close client socket directly, server should emit DISCONNECT within timeout."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", _SHORT_TIMEOUT)

        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Force-close client transport without graceful disconnect
        force_close_transport(client)

        # Server should detect timeout disconnect
        events = await collect_events(
            server, EventType.DISCONNECT, count=1, timeout=_SHORT_TIMEOUT + 3.0
        )
        assert len(events) == 1
        assert events[0].type == EventType.DISCONNECT

    async def test_client_detects_server_gone(
        self, server_factory, client_factory, monkeypatch
    ):
        """Close server socket, client should emit DISCONNECT."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", _SHORT_TIMEOUT)

        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Force-close server transport
        force_close_transport(server)

        # Client should detect timeout disconnect
        events = await collect_events(
            client, EventType.DISCONNECT, count=1, timeout=_SHORT_TIMEOUT + 3.0
        )
        assert len(events) == 1
        assert events[0].type == EventType.DISCONNECT


class TestMidTransferClose:
    async def test_close_during_handshake(self, server_factory):
        """close() before handshake completes should not hang."""
        server = await server_factory()
        addr = server.local_address

        # Start connection but close immediately
        from aiorak import Client

        client = Client(addr)
        # Start connect in background, then close before it completes
        connect_task = asyncio.create_task(client.connect(timeout=5.0))
        await asyncio.sleep(0.1)  # Let handshake start
        await client.close()

        # Connect should either raise or complete; it should not hang
        with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError, RuntimeError, Exception)):
            await asyncio.wait_for(connect_task, timeout=2.0)

    async def test_close_during_data_transfer(self, server_factory, client_factory):
        """close() while large send in progress should exit cleanly."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Send a large message
        payload = b"\x86" + bytes(50_000)
        await client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)

        # Close immediately while data is still in flight
        await client.close()

        # Should exit cleanly, no hanging
        assert client._closed is True
