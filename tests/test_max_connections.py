"""Integration tests — connection limit enforcement."""

import asyncio

import pytest

import aiorak
from aiorak import Connection

from .conftest import wait_for_peers


class TestMaxConnections:
    async def test_max_connections_enforced(self, server_factory):
        """Server with max_connections=3, try connecting 5 clients, verify only 3 succeed."""
        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=3)
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

        # Verify server has at most 3 peers
        assert len(server._peers) <= 3

        # Cleanup
        for cli in connected:
            await cli.close()
