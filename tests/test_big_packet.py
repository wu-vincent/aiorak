"""Big packet tests — splitting and reassembly of large messages."""

import asyncio
import hashlib
import os

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import collect_packets, wait_for_peers


def _make_deterministic_payload(size: int, seed: int = 0) -> bytes:
    """Create a deterministic repeating-pattern payload."""
    header = b"\x86"
    body_size = size - 1
    pattern = bytes(range(256))
    repeats = body_size // 256 + 1
    body = (pattern * repeats)[:body_size]
    return header + body


class TestBigPacket:
    @pytest.mark.parametrize("size", [50_000, 500_000])
    async def test_large_reliable_ordered(self, server_factory, client_factory, size):
        """Send a large payload, verify exact byte match."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = _make_deterministic_payload(size)
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        timeout = 30.0 if size >= 500_000 else 15.0
        data = await asyncio.wait_for(received.get(), timeout=timeout)
        assert data == payload

    @pytest.mark.slow
    async def test_very_large_5mb(self, server_factory, client_factory):
        """5MB RELIABLE_ORDERED — verify via SHA256."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        size = 5 * 1024 * 1024
        payload = _make_deterministic_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        data = await asyncio.wait_for(received.get(), timeout=60.0)
        actual_hash = hashlib.sha256(data).hexdigest()
        assert actual_hash == expected_hash

    async def test_large_reliable_no_ordering(self, server_factory, client_factory):
        """100KB with RELIABLE, verify bytes match."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = _make_deterministic_payload(100_000)
        await client.send(payload, reliability=Reliability.RELIABLE)

        data = await asyncio.wait_for(received.get(), timeout=15.0)
        assert data == payload

    async def test_multiple_large_sequential(self, server_factory, client_factory):
        """3 x 100KB in sequence, verify all arrive correctly."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payloads = [_make_deterministic_payload(100_000 + i) for i in range(3)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        items = []
        for _ in range(3):
            items.append(await asyncio.wait_for(received.get(), timeout=30.0))
        for i, data in enumerate(items):
            assert data == payloads[i]

    async def test_large_bidirectional(self, server_factory, client_factory):
        """Client sends 50KB, server sends 50KB simultaneously."""
        server_received = asyncio.Queue()
        server_payload = b"\x86" + bytes([0xAB]) * 49_999

        async def handler(conn: Connection):
            await conn.send(server_payload, reliability=Reliability.RELIABLE_ORDERED)
            async for data in conn:
                server_received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        client_payload = _make_deterministic_payload(50_000)
        await client.send(client_payload, reliability=Reliability.RELIABLE_ORDERED)

        # Collect from both sides
        srv_data = asyncio.wait_for(server_received.get(), timeout=15.0)
        cli_data = collect_packets(client, count=1, timeout=15.0)
        srv_result, cli_result = await asyncio.gather(srv_data, cli_data)

        assert srv_result == client_payload
        assert len(cli_result) == 1
        assert cli_result[0] == server_payload
