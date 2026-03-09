"""Burst pattern tests — rapid message sending with various sizes and modes."""

import asyncio
import struct

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import collect_packets, wait_for_peers


def _burst_payload(index: int, size: int) -> bytes:
    header = b"\x86" + struct.pack(">I", index)
    if size <= len(header):
        return header[:size]
    body = bytes([index & 0xFF]) * (size - len(header))
    return header + body


def _make_collecting_handler(queue: asyncio.Queue):
    async def handler(conn: Connection):
        async for data in conn:
            queue.put_nowait(data)
    return handler


async def _collect_from_queue(queue: asyncio.Queue, count: int, timeout: float = 5.0) -> list[bytes]:
    items = []
    async def _gather():
        while len(items) < count:
            items.append(await queue.get())
    try:
        await asyncio.wait_for(_gather(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return items


class TestBurstPatterns:
    async def test_small_burst_64b_x_128(self, server_factory, client_factory):
        """128 x 64B RELIABLE_ORDERED, all arrive in order."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 128
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.RELIABLE_ORDERED
            )

        items = await _collect_from_queue(received, count, timeout=10.0)
        assert len(items) == count

        for i, data in enumerate(items):
            idx = struct.unpack(">I", data[1:5])[0]
            assert idx == i, f"Expected index {i}, got {idx}"

    async def test_medium_burst_512b_x_64(self, server_factory, client_factory):
        """64 x 512B, ordering + data integrity."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 64
        payloads = [_burst_payload(i, 512) for i in range(count)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        items = await _collect_from_queue(received, count, timeout=10.0)
        assert len(items) == count

        for i, data in enumerate(items):
            assert data == payloads[i]

    async def test_large_burst_4kb_x_16(self, server_factory, client_factory):
        """16 x 4KB (some may split), all arrive."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 16
        payloads = [_burst_payload(i, 4096) for i in range(count)]
        for p in payloads:
            await client.send(p, reliability=Reliability.RELIABLE_ORDERED)

        items = await _collect_from_queue(received, count, timeout=15.0)
        assert len(items) == count

        for i, data in enumerate(items):
            assert data == payloads[i]

    async def test_burst_unreliable(self, server_factory, client_factory):
        """128 x 64B UNRELIABLE, assert >= 90% arrive on loopback."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 128
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.UNRELIABLE
            )

        items = await _collect_from_queue(received, count, timeout=5.0)
        assert len(items) >= int(count * 0.9), (
            f"Only {len(items)}/{count} arrived (expected >= 90%)"
        )

    async def test_repeated_bursts(self, server_factory, client_factory):
        """3 bursts of 50 msgs with 0.5s gaps, verify total count."""
        received = asyncio.Queue()
        server = await server_factory(handler=_make_collecting_handler(received))
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        total = 0
        for burst in range(3):
            for i in range(50):
                idx = burst * 50 + i
                await client.send(
                    _burst_payload(idx, 64), reliability=Reliability.RELIABLE_ORDERED
                )
                total += 1
            await asyncio.sleep(0.5)

        items = await _collect_from_queue(received, total, timeout=15.0)
        assert len(items) == total

    async def test_burst_bidirectional(self, server_factory, client_factory):
        """Both sides send 64 msgs simultaneously."""
        server_received = asyncio.Queue()

        async def handler(conn: Connection):
            # Send server->client messages
            for i in range(64):
                await conn.send(
                    _burst_payload(i + 1000, 64),
                    reliability=Reliability.RELIABLE_ORDERED,
                )
            # Collect client->server messages
            async for data in conn:
                server_received.put_nowait(data)

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        count = 64
        for i in range(count):
            await client.send(
                _burst_payload(i, 64), reliability=Reliability.RELIABLE_ORDERED
            )

        server_items, client_items = await asyncio.gather(
            _collect_from_queue(server_received, count, timeout=10.0),
            collect_packets(client, count=count, timeout=10.0),
        )

        assert len(server_items) == count
        assert len(client_items) == count
