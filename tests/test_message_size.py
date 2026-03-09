"""Integration tests — variable stride delivery."""

import asyncio
import struct

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import wait_for_peers


def _make_payload(size: int, fill: int) -> bytes:
    """Create a test payload: user ID + 2-byte size + fill pattern."""
    header = b"\x86" + struct.pack(">H", size)
    body = bytes([fill & 0xFF] * (size - len(header)))
    return header + body


class TestMessageSize:
    @pytest.mark.parametrize("size", [10, 100, 500, 1000, 1500, 2000])
    async def test_representative_strides(self, server_factory, client_factory, size):
        """Parametrized test for various message sizes — verify byte-level data integrity."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        fill = size & 0xFF
        payload = _make_payload(size, fill)
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        data = await asyncio.wait_for(received.get(), timeout=5.0)
        assert data == payload

    async def test_exact_mtu_boundary(self, server_factory, client_factory):
        """Message at approximately MTU payload size."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = b"\x86" + bytes(range(256)) * 5 + bytes(range(155))
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        data = await asyncio.wait_for(received.get(), timeout=5.0)
        assert data == payload

    async def test_large_message_split_integrity(self, server_factory, client_factory):
        """5000+ bytes message — verify reassembled data matches."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = b"\x86" + bytes(range(256)) * 20
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        data = await asyncio.wait_for(received.get(), timeout=10.0)
        assert data == payload
