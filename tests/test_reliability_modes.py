"""End-to-end tests for all reliability modes.

Adapted from LoopbackPerformanceTest.cpp patterns. Fills the biggest gap:
only RELIABLE_ORDERED was previously tested end-to-end.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


def _payload(index: int, size: int = 32) -> bytes:
    """Create a test payload: user ID + big-endian index + fill."""
    header = b"\x86" + struct.pack(">I", index)
    body = bytes([index & 0xFF]) * (size - len(header))
    return header + body


class TestUnreliable:
    async def test_unreliable_delivery(self, server_factory, client_factory):
        """50 msgs UNRELIABLE, assert at least 1 arrives (loopback: expect most)."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 50
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.UNRELIABLE)

        # On loopback, most should arrive. Require at least 1.
        events = []
        try:
            events = await collect_events(server, EventType.RECEIVE, count=count, timeout=5.0)
        except asyncio.TimeoutError:
            pass
        assert len(events) >= 1

    async def test_unreliable_data_integrity(self, server_factory, client_factory):
        """Verify payload bytes match for unreliable delivery."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        payload = _payload(42, size=64)
        await client.send(payload, reliability=Reliability.UNRELIABLE)

        events = await collect_events(server, EventType.RECEIVE, count=1, timeout=5.0)
        assert events[0].data == payload


class TestReliable:
    async def test_reliable_all_arrive(self, server_factory, client_factory):
        """30 msgs RELIABLE, all must arrive."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 30
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.RELIABLE)

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count

    async def test_reliable_data_integrity(self, server_factory, client_factory):
        """Byte-level match for RELIABLE delivery."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        payloads = [_payload(i, size=128) for i in range(10)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE)

        events = await collect_events(server, EventType.RECEIVE, count=10, timeout=10.0)
        received = {e.data for e in events}
        for p in payloads:
            assert p in received


class TestUnreliableSequenced:
    async def test_drops_stale_on_loopback(self, server_factory, client_factory):
        """Received seq indices should be monotonically non-decreasing."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 30
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.UNRELIABLE_SEQUENCED)

        events = []
        try:
            events = await collect_events(server, EventType.RECEIVE, count=count, timeout=5.0)
        except asyncio.TimeoutError:
            pass

        assert len(events) >= 1
        # Extract indices and verify monotonic non-decreasing
        indices = [struct.unpack(">I", e.data[1:5])[0] for e in events]
        for i in range(1, len(indices)):
            assert indices[i] >= indices[i - 1], (
                f"Non-monotonic: index {indices[i]} after {indices[i-1]}"
            )


class TestReliableSequenced:
    async def test_reliable_sequenced_delivery(self, server_factory, client_factory):
        """Verify delivery with no stale packets."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.RELIABLE_SEQUENCED)

        events = []
        try:
            events = await collect_events(server, EventType.RECEIVE, count=count, timeout=5.0)
        except asyncio.TimeoutError:
            pass

        assert len(events) >= 1
        # Indices should be monotonically non-decreasing
        indices = [struct.unpack(">I", e.data[1:5])[0] for e in events]
        for i in range(1, len(indices)):
            assert indices[i] >= indices[i - 1]


class TestAckReceiptModes:
    async def test_reliable_ordered_with_ack_receipt(self, server_factory, client_factory):
        """Delivers in order like RELIABLE_ORDERED."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT
            )

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count
        for i, e in enumerate(events):
            expected_idx = struct.unpack(">I", e.data[1:5])[0]
            assert expected_idx == i

    async def test_reliable_with_ack_receipt(self, server_factory, client_factory):
        """All arrive like RELIABLE."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.RELIABLE_WITH_ACK_RECEIPT
            )

        events = await collect_events(server, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count

    async def test_unreliable_with_ack_receipt(self, server_factory, client_factory):
        """At least some arrive."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.UNRELIABLE_WITH_ACK_RECEIPT
            )

        events = []
        try:
            events = await collect_events(server, EventType.RECEIVE, count=count, timeout=5.0)
        except asyncio.TimeoutError:
            pass
        assert len(events) >= 1


class TestMixedReliability:
    async def test_interleaved_modes(self, server_factory, client_factory):
        """RELIABLE + RELIABLE_ORDERED + UNRELIABLE in same connection."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        modes = [
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.UNRELIABLE,
        ]

        sent = 0
        reliable_count = 0
        for i in range(30):
            mode = modes[i % 3]
            await client.send(_payload(i), reliability=mode)
            sent += 1
            if mode != Reliability.UNRELIABLE:
                reliable_count += 1

        # At minimum, all reliable messages should arrive
        events = []
        try:
            events = await collect_events(
                server, EventType.RECEIVE, count=sent, timeout=10.0
            )
        except asyncio.TimeoutError:
            pass
        assert len(events) >= reliable_count

    async def test_server_to_client_reliable(self, server_factory, client_factory):
        """Server to client direction with RELIABLE."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        connect_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        client_addr = connect_events[0].address

        count = 20
        for i in range(count):
            await server.send(client_addr, _payload(i), reliability=Reliability.RELIABLE)

        events = await collect_events(client, EventType.RECEIVE, count=count, timeout=10.0)
        assert len(events) == count


class TestMultiChannelAllModes:
    async def test_ordered_channel_independence(self, server_factory, client_factory):
        """4 channels x 10 msgs, per-channel ordering preserved."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        n_channels = 4
        msgs_per_channel = 10
        total = n_channels * msgs_per_channel

        for i in range(msgs_per_channel):
            for ch in range(n_channels):
                idx = ch * msgs_per_channel + i
                await client.send(
                    _payload(idx),
                    reliability=Reliability.RELIABLE_ORDERED,
                    channel=ch,
                )

        events = await collect_events(server, EventType.RECEIVE, count=total, timeout=10.0)
        assert len(events) == total

        # Group by channel-derived index and verify per-channel ordering
        per_channel: dict[int, list[int]] = {ch: [] for ch in range(n_channels)}
        for e in events:
            idx = struct.unpack(">I", e.data[1:5])[0]
            ch = idx // msgs_per_channel
            if ch < n_channels:
                per_channel[ch].append(idx)

        for ch in range(n_channels):
            indices = per_channel[ch]
            assert indices == sorted(indices), f"Channel {ch} out of order: {indices}"
