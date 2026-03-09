"""Integration tests adapted from MessageSizeTest.cpp — variable stride delivery."""

from __future__ import annotations

import asyncio
import struct

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _make_payload(size: int, fill: int) -> bytes:
    """Create a test payload: user ID + 2-byte size + fill pattern."""
    header = b"\x86" + struct.pack(">H", size)
    body = bytes([fill & 0xFF] * (size - len(header)))
    return header + body


class TestMessageSize:
    @pytest.mark.parametrize("size", [10, 100, 500, 1000, 1500, 2000])
    async def test_representative_strides(self, server_factory, client_factory, size):
        """Parametrized test for various message sizes — verify byte-level data integrity."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        fill = size & 0xFF
        payload = _make_payload(size, fill)
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
        assert len(events) == 1
        assert events[0].data == payload

    async def test_exact_mtu_boundary(self, server_factory, client_factory):
        """Message at approximately MTU payload size."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # MTU is 1492, minus overhead (~60 bytes for headers) ≈ 1432 max single frame
        payload = b"\x86" + bytes(range(256)) * 5 + bytes(range(155))  # ~1435 bytes
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
        assert events[0].data == payload

    async def test_large_message_split_integrity(self, server_factory, client_factory):
        """5000+ bytes message — verify reassembled data matches."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        # Build a large payload with a recognizable pattern
        payload = b"\x86" + bytes(range(256)) * 20  # ~5121 bytes
        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=10.0)
        assert len(events) == 1
        assert events[0].data == payload
