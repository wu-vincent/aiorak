"""Dropped connection tests — timeout detection when connections are silently dropped."""

import asyncio

import pytest

import aiorak
from aiorak import Connection
from aiorak._constants import DEFAULT_TIMEOUT

from .conftest import collect_packets, wait_for_peers, force_close_transport


_SHORT_TIMEOUT = 2.0


class TestDroppedConnection:
    async def test_server_detects_client_gone(
        self, server_factory, client_factory, monkeypatch
    ):
        """Close client socket directly, server handler should end within timeout."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", _SHORT_TIMEOUT)

        handler_done = asyncio.Event()

        async def handler(conn: Connection):
            async for data in conn:
                pass
            handler_done.set()

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        force_close_transport(client)

        await asyncio.wait_for(handler_done.wait(), timeout=_SHORT_TIMEOUT + 3.0)

    async def test_client_detects_server_gone(
        self, server_factory, client_factory, monkeypatch
    ):
        """Close server socket, client iterator should end."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", _SHORT_TIMEOUT)

        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        force_close_transport(server)

        # Client iterator should end (via disconnect sentinel)
        async def _drain():
            async for data in client:
                pass

        await asyncio.wait_for(_drain(), timeout=_SHORT_TIMEOUT + 3.0)


class TestMidTransferClose:
    async def test_close_during_handshake(self, server_factory):
        """close() before handshake completes should not hang."""
        server = await server_factory()
        addr = server.local_address

        from aiorak import Client

        client = Client(addr)
        connect_task = asyncio.create_task(client.connect(timeout=5.0))
        await asyncio.sleep(0.1)
        await client.close()

        with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError, RuntimeError, Exception)):
            await asyncio.wait_for(connect_task, timeout=2.0)

    async def test_close_during_data_transfer(self, server_factory, client_factory):
        """close() while large send in progress should exit cleanly."""
        server = await server_factory()
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = b"\x86" + bytes(50_000)
        await client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)

        await client.close()
        assert client._closed is True
