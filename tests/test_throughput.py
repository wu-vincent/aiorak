"""Throughput and relay tests."""

import asyncio
import struct
import time

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import collect_packets, wait_for_peers


def _throughput_payload(index: int, size: int = 1024) -> bytes:
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
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 500
        payload_size = 1024

        start = time.monotonic()
        for i in range(count):
            await client.send(
                _throughput_payload(i, payload_size), reliability=reliability
            )

        items = []
        async def _collect():
            while len(items) < count:
                items.append(await received.get())

        if reliability == Reliability.UNRELIABLE:
            try:
                await asyncio.wait_for(_collect(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            received_count = len(items)
        else:
            await asyncio.wait_for(_collect(), timeout=15.0)
            received_count = len(items)
            assert received_count == count

        elapsed = time.monotonic() - start
        total_bytes = received_count * payload_size
        throughput = total_bytes / elapsed if elapsed > 0 else float("inf")

        assert throughput > 500 * 1024, (
            f"Throughput too low: {throughput / 1024:.1f} KB/s "
            f"({received_count} msgs in {elapsed:.2f}s)"
        )

    async def test_sustained_5_seconds(self, server_factory, client_factory):
        """Continuous 512B sends for 5s, assert minimum count."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

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
            if sent % 50 == 0:
                await asyncio.sleep(0)

        items = []
        async def _collect():
            while len(items) < sent:
                items.append(await received.get())

        await asyncio.wait_for(_collect(), timeout=15.0)
        assert len(items) == sent
        assert sent >= 100, f"Only sent {sent} messages in {duration}s"


class TestRelay:
    async def test_relay_forwarding(self, server_factory, client_factory):
        """Client A sends to server, server forwards to client B."""
        relay_queue = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                # Forward received data to the relay queue
                relay_queue.put_nowait((conn.address, data))

        server = await server_factory(handler=handler, max_connections=2)

        client_a = await client_factory(server.local_address)
        client_b = await client_factory(server.local_address)

        await wait_for_peers(server, 2, timeout=5.0)

        # Client A sends data
        payload = b"\x86relay_test_data_12345"
        await client_a.send(payload)

        # Server receives from A
        addr_from, data = await asyncio.wait_for(relay_queue.get(), timeout=5.0)
        assert data == payload

        # Find peer B and forward
        for peer in server._peers.values():
            if peer.address != addr_from:
                await peer.send(data)
                break

        # Client B receives the forwarded data
        b_packets = await collect_packets(client_b, count=1, timeout=5.0)
        assert b_packets[0] == payload
