"""End-to-end tests for all reliability modes."""

import asyncio
import struct

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import wait_for_peers


def _payload(index: int, size: int = 32) -> bytes:
    """Create a test payload: user ID + big-endian index + fill."""
    header = b"\x86" + struct.pack(">I", index)
    body = bytes([index & 0xFF]) * (size - len(header))
    return header + body


def _make_collecting_handler(queue: asyncio.Queue):
    """Return a handler that collects received data into the queue."""
    async def handler(conn: Connection):
        async for data in conn:
            queue.put_nowait(data)
    return handler


async def _collect_from_queue(queue: asyncio.Queue, count: int, timeout: float = 5.0) -> list[bytes]:
    """Collect count items from queue within timeout."""
    items = []
    async def _gather():
        while len(items) < count:
            items.append(await queue.get())
    try:
        await asyncio.wait_for(_gather(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return items


class TestUnreliable:
    async def test_unreliable_delivery(self, server_factory, client_factory):
        """50 msgs UNRELIABLE, assert at least 1 arrives (loopback: expect most)."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 50
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.UNRELIABLE)

        items = await _collect_from_queue(received, count, timeout=5.0)
        assert len(items) >= 1

    async def test_unreliable_data_integrity(self, server_factory, client_factory):
        """Verify payload bytes match for unreliable delivery."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payload = _payload(42, size=64)
        await client.send(payload, reliability=Reliability.UNRELIABLE)

        items = await _collect_from_queue(received, 1, timeout=5.0)
        assert items[0] == payload


class TestReliable:
    async def test_reliable_all_arrive(self, server_factory, client_factory):
        """30 msgs RELIABLE, all must arrive."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 30
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.RELIABLE)

        items = await _collect_from_queue(received, count, timeout=10.0)
        assert len(items) == count

    async def test_reliable_data_integrity(self, server_factory, client_factory):
        """Byte-level match for RELIABLE delivery."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        payloads = [_payload(i, size=128) for i in range(10)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE)

        items = await _collect_from_queue(received, 10, timeout=10.0)
        received_set = set(items)
        for p in payloads:
            assert p in received_set


class TestUnreliableSequenced:
    async def test_drops_stale_on_loopback(self, server_factory, client_factory):
        """Received seq indices should be monotonically non-decreasing."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 30
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.UNRELIABLE_SEQUENCED)

        items = await _collect_from_queue(received, count, timeout=5.0)
        assert len(items) >= 1
        indices = [struct.unpack(">I", e[1:5])[0] for e in items]
        for i in range(1, len(indices)):
            assert indices[i] >= indices[i - 1], (
                f"Non-monotonic: index {indices[i]} after {indices[i-1]}"
            )


class TestReliableSequenced:
    async def test_reliable_sequenced_delivery(self, server_factory, client_factory):
        """Verify delivery with no stale packets."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(_payload(i), reliability=Reliability.RELIABLE_SEQUENCED)

        items = await _collect_from_queue(received, count, timeout=5.0)
        assert len(items) >= 1
        indices = [struct.unpack(">I", e[1:5])[0] for e in items]
        for i in range(1, len(indices)):
            assert indices[i] >= indices[i - 1]


class TestAckReceiptModes:
    async def test_reliable_ordered_with_ack_receipt(self, server_factory, client_factory):
        """Delivers in order like RELIABLE_ORDERED."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT
            )

        items = await _collect_from_queue(received, count, timeout=10.0)
        assert len(items) == count
        for i, data in enumerate(items):
            expected_idx = struct.unpack(">I", data[1:5])[0]
            assert expected_idx == i

    async def test_reliable_with_ack_receipt(self, server_factory, client_factory):
        """All arrive like RELIABLE."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.RELIABLE_WITH_ACK_RECEIPT
            )

        items = await _collect_from_queue(received, count, timeout=10.0)
        assert len(items) == count

    async def test_unreliable_with_ack_receipt(self, server_factory, client_factory):
        """At least some arrive."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 20
        for i in range(count):
            await client.send(
                _payload(i), reliability=Reliability.UNRELIABLE_WITH_ACK_RECEIPT
            )

        items = await _collect_from_queue(received, count, timeout=5.0)
        assert len(items) >= 1


class TestMixedReliability:
    async def test_interleaved_modes(self, server_factory, client_factory):
        """RELIABLE + RELIABLE_ORDERED + UNRELIABLE in same connection."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

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

        items = await _collect_from_queue(received, sent, timeout=10.0)
        assert len(items) >= reliable_count

    async def test_server_to_client_reliable(self, server_factory, client_factory):
        """Server to client direction with RELIABLE."""
        send_queue = asyncio.Queue()

        async def handler(conn: Connection):
            # Wait for all payloads to be queued then send them
            while True:
                try:
                    payload = send_queue.get_nowait()
                    await conn.send(payload, reliability=Reliability.RELIABLE)
                except asyncio.QueueEmpty:
                    break
            # Keep handler alive to maintain connection
            async for data in conn:
                pass

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 20
        # Get the peer and send directly
        peer = list(server._peers.values())[0]
        for i in range(count):
            await peer.send(_payload(i), reliability=Reliability.RELIABLE)

        from .conftest import collect_packets
        packets = await collect_packets(client, count=count, timeout=10.0)
        assert len(packets) == count


class TestMultiChannelAllModes:
    async def test_ordered_channel_independence(self, server_factory, client_factory):
        """4 channels x 10 msgs, per-channel ordering preserved."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

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

        items = await _collect_from_queue(received, total, timeout=10.0)
        assert len(items) == total

        per_channel: dict[int, list[int]] = {ch: [] for ch in range(n_channels)}
        for data in items:
            idx = struct.unpack(">I", data[1:5])[0]
            ch = idx // msgs_per_channel
            if ch < n_channels:
                per_channel[ch].append(idx)

        for ch in range(n_channels):
            indices = per_channel[ch]
            assert indices == sorted(indices), f"Channel {ch} out of order: {indices}"
