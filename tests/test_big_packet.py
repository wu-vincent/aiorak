"""Big packet tests — adapted from BigPacketTest.cpp.

Tests splitting and reassembly of large messages (50KB, 500KB, 5MB).
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _make_payload(size: int) -> bytes:
    """Create a deterministic test payload of the given size."""
    # Use os.urandom for realistic data, prefix with user packet ID
    data = b"\x86" + os.urandom(size - 1)
    return data


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
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        payload = _make_deterministic_payload(size)

        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        timeout = 30.0 if size >= 500_000 else 15.0
        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=timeout)
        assert len(events) == 1
        assert events[0].data == payload

    @pytest.mark.slow
    async def test_very_large_5mb(self, server_factory, client_factory):
        """5MB RELIABLE_ORDERED — verify via SHA256."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        size = 5 * 1024 * 1024
        payload = _make_deterministic_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=60.0)
        assert len(events) == 1
        actual_hash = hashlib.sha256(events[0].data).hexdigest()
        assert actual_hash == expected_hash

    async def test_large_reliable_no_ordering(self, server_factory, client_factory):
        """100KB with RELIABLE, verify bytes match."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        payload = _make_deterministic_payload(100_000)
        await client.send(payload, reliability=Reliability.RELIABLE)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=15.0)
        assert len(events) == 1
        assert events[0].data == payload

    async def test_multiple_large_sequential(self, server_factory, client_factory):
        """3 x 100KB in sequence, verify all arrive correctly."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        payloads = [_make_deterministic_payload(100_000 + i) for i in range(3)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=3, timeout=30.0)
        assert len(events) == 3
        for i, e in enumerate(events):
            assert e.data == payloads[i]

    async def test_large_bidirectional(self, server_factory, client_factory):
        """Client sends 50KB, server sends 50KB simultaneously."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        connect_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        client_addr = connect_events[0].address

        client_payload = _make_deterministic_payload(50_000)
        server_payload = b"\x86" + bytes([0xAB]) * 49_999

        # Send simultaneously
        await client.send(client_payload, reliability=Reliability.RELIABLE_ORDERED)
        await server.send(client_addr, server_payload, reliability=Reliability.RELIABLE_ORDERED)

        # Collect from both sides
        server_events, client_events = await asyncio.gather(
            collect_events(server, EventType.RECEIVE, count=1, timeout=15.0),
            collect_events(client, EventType.RECEIVE, count=1, timeout=15.0),
        )

        assert len(server_events) == 1
        assert server_events[0].data == client_payload
        assert len(client_events) == 1
        assert client_events[0].data == server_payload
