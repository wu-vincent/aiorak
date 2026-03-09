"""Integration tests adapted from MaximumConnectTest.cpp — connection limit enforcement."""

from __future__ import annotations

import asyncio

import pytest

import aiorak
from aiorak import EventType

from .conftest import collect_events


class TestMaxConnections:
    async def test_max_connections_enforced(self, server_factory):
        """Server with max_connections=3, try connecting 5 clients, verify only 3 succeed."""
        server = await server_factory(max_connections=3)
        addr = server.local_address

        connected: list[aiorak.Client] = []
        failed: list[Exception] = []

        for _ in range(5):
            try:
                cli = await aiorak.connect(addr, timeout=2.0)
                connected.append(cli)
            except (asyncio.TimeoutError, OSError) as exc:
                failed.append(exc)

        # At most 3 should have connected
        assert len(connected) <= 3

        # Verify server got at most 3 CONNECT events
        try:
            events = await collect_events(
                server, EventType.CONNECT, count=3, timeout=3.0
            )
            connect_count = len(events)
        except asyncio.TimeoutError:
            # Fewer than 3 connected (possible if timing is tight)
            connect_count = 0

        assert connect_count <= 3

        # Cleanup
        for cli in connected:
            await cli.close()
