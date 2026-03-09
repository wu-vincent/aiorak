"""Burst pattern tests — adapted from BurstTest.cpp.

Tests rapid message sending patterns with various sizes and modes.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _burst_payload(index: int, size: int) -> bytes:
    """Create a burst test payload with index and fill to target size."""
    header = b"\x86" + struct.pack(">I", index)
    if size <= len(header):
        return header[:size]
    body = bytes([index & 0xFF]) * (size - len(header))
    return header + body


class TestBurstPatterns:
    async def test_small_burst_64b_x_128(self, server_factory, client_factory):
        """128 x 64B RELIABLE_ORDERED, all arrive in order."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 128
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.RELIABLE_ORDERED
            )

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count

        # Verify ordering
        for i, e in enumerate(events):
            idx = struct.unpack(">I", e.data[1:5])[0]
            assert idx == i, f"Expected index {i}, got {idx}"

    async def test_medium_burst_512b_x_64(self, server_factory, client_factory):
        """64 x 512B, ordering + data integrity."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 64
        payloads = [_burst_payload(i, 512) for i in range(count)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count

        for i, e in enumerate(events):
            assert e.data == payloads[i]

    async def test_large_burst_4kb_x_16(self, server_factory, client_factory):
        """16 x 4KB (some may split), all arrive."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 16
        payloads = [_burst_payload(i, 4096) for i in range(count)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=15.0)
        assert len(events) == count

        for i, e in enumerate(events):
            assert e.data == payloads[i]

    async def test_burst_unreliable(self, server_factory, client_factory):
        """128 x 64B UNRELIABLE, assert >= 90% arrive on loopback."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 128
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.UNRELIABLE
            )

        events = []
        try:
            events = await collect_events(
                server, EventType.RECEIVE, count=count, timeout=5.0
            )
        except asyncio.TimeoutError:
            pass

        # On loopback, expect >= 90% delivery
        assert len(events) >= int(count * 0.9), (
            f"Only {len(events)}/{count} arrived (expected >= 90%)"
        )

    async def test_repeated_bursts(self, server_factory, client_factory):
        """3 bursts of 50 msgs with 0.5s gaps, verify total count."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        total = 0
        for burst in range(3):
            for i in range(50):
                idx = burst * 50 + i
                await client.send(
                    _burst_payload(idx, 64), reliability=Reliability.RELIABLE_ORDERED
                )
                total += 1
            await asyncio.sleep(0.5)

        events = await collect_events(server, EventType.RECEIVE, count=total, timeout=15.0)
        assert len(events) == total

    async def test_burst_bidirectional(self, server_factory, client_factory):
        """Both sides send 64 msgs simultaneously."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        connect_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        client_addr = connect_events[0].address

        count = 64

        # Send from both sides
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.RELIABLE_ORDERED
            )
            await server.send(
                client_addr,
                _burst_payload(i + 1000, 64),
                reliability=Reliability.RELIABLE_ORDERED,
            )

        server_events, client_events = await asyncio.gather(
            collect_events(server, EventType.RECEIVE, count=count, timeout=10.0),
            collect_events(client, EventType.RECEIVE, count=count, timeout=10.0),
        )

        assert len(server_events) == count
        assert len(client_events) == count
