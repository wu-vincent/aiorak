"""Throughput and relay tests — adapted from LoopbackPerformanceTest.cpp
and FlowControlTest.cpp.

Measures data transfer rates on loopback and tests relay forwarding.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _throughput_payload(index: int, size: int = 1024) -> bytes:
    """Create a throughput test payload."""
    header = b"\x86" + struct.pack(">I", index)
    body = bytes([index & 0xFF]) * (size - len(header))
    return header + body


class TestThroughput:
    @pytest.mark.parametrize(
        "reliability",
        [Reliability.RELIABLE_ORDERED, Reliability.RELIABLE, Reliability.UNRELIABLE],
    )
    async def test_loopback_throughput(
        self, server_factory, client_factory, reliability
    ):
        """500 x 1KB messages, assert >500KB/s on loopback."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 500
        payload_size = 1024

        start = time.monotonic()
        for i in range(count):
            await client.send(
                _throughput_payload(i, payload_size), reliability=reliability
            )

        # For unreliable, we may not get all messages
        if reliability == Reliability.UNRELIABLE:
            events = []
            try:
                events = await collect_events(
                    server, EventType.RECEIVE, count=count, timeout=10.0
                )
            except asyncio.TimeoutError:
                pass
            received = len(events)
        else:
            events = await collect_events(
                server, EventType.RECEIVE, count=count, timeout=15.0
            )
            received = len(events)
            assert received == count

        elapsed = time.monotonic() - start
        total_bytes = received * payload_size
        throughput = total_bytes / elapsed if elapsed > 0 else float("inf")

        # Assert minimum throughput: 500 KB/s on loopback
        assert throughput > 500 * 1024, (
            f"Throughput too low: {throughput / 1024:.1f} KB/s "
            f"({received} msgs in {elapsed:.2f}s)"
        )

    async def test_sustained_5_seconds(self, server_factory, client_factory):
        """Continuous 512B sends for 5s, assert minimum count."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        duration = 5.0
        payload_size = 512
        sent = 0
        start = time.monotonic()

        while time.monotonic() - start < duration:
            await client.send(
                _throughput_payload(sent, payload_size),
                reliability=Reliability.RELIABLE_ORDERED,
            )
            sent += 1
            # Yield to event loop periodically
            if sent % 50 == 0:
                await asyncio.sleep(0)

        # Collect as many as we can
        events = await collect_events(
            server, EventType.RECEIVE, count=sent, timeout=15.0
        )
        assert len(events) == sent

        # Should have sent a meaningful number of messages
        assert sent >= 100, f"Only sent {sent} messages in {duration}s"


class TestRelay:
    async def test_relay_forwarding(self, server_factory, client_factory):
        """Client A sends to server, server forwards to client B."""
        server = await server_factory(max_connections=2)
        addr = server.local_address

        client_a = await client_factory(addr)
        client_b = await client_factory(addr)

        connect_events = await collect_events(
            server, EventType.CONNECT, count=2, timeout=5.0
        )

        # Identify client addresses from server's perspective
        addr_a = connect_events[0].address
        addr_b = connect_events[1].address

        # Client A sends data
        payload = b"\x86relay_test_data_12345"
        await client_a.send(payload)

        # Server receives from A
        recv = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
        assert recv[0].data == payload

        # Server forwards to B
        await server.send(addr_b, recv[0].data)

        # Client B receives the forwarded data
        b_recv = await collect_events(client_b, EventType.RECEIVE, count=1, timeout=5.0)
        assert b_recv[0].data == payload
