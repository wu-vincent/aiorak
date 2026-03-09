"""Integration tests — multi-channel ordering."""

import asyncio
import struct

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import wait_for_peers


class TestReliableOrdered:
    async def test_packets_arrive_in_order_single_channel(self, server_factory, client_factory):
        """20 packets on channel 0, verify ordering."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        num_packets = 20
        for i in range(num_packets):
            payload = b"\x86" + struct.pack(">I", i)
            await client.send(payload, reliability=Reliability.RELIABLE_ORDERED, channel=0)

        seqs = []
        async def _collect():
            while len(seqs) < num_packets:
                data = await received.get()
                seq = struct.unpack(">I", data[1:])[0]
                seqs.append(seq)

        await asyncio.wait_for(_collect(), timeout=10.0)
        assert seqs == list(range(num_packets))

    async def test_packets_arrive_in_order_multi_channel(self, server_factory, client_factory):
        """4 channels x 10 packets, verify per-channel ordering."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        num_channels = 4
        num_packets = 10

        for ch in range(num_channels):
            for i in range(num_packets):
                payload = b"\x86" + struct.pack(">BH", ch, i)
                await client.send(payload, reliability=Reliability.RELIABLE_ORDERED, channel=ch)

        per_channel: dict[int, list[int]] = {ch: [] for ch in range(num_channels)}
        total = num_channels * num_packets

        async def _collect():
            count = 0
            while count < total:
                data = await received.get()
                ch, seq = struct.unpack(">BH", data[1:])
                per_channel[ch].append(seq)
                count += 1

        await asyncio.wait_for(_collect(), timeout=10.0)

        for ch in range(num_channels):
            assert per_channel[ch] == list(range(num_packets)), f"Channel {ch} out of order"

    async def test_variable_size_ordering(self, server_factory, client_factory):
        """Packets with sizes 10-2000 bytes, verify ordering preserved."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address
        client = await client_factory(addr)
        await wait_for_peers(server, 1, timeout=3.0)

        sizes = [10, 50, 100, 500, 1000, 1500, 2000]
        for i, size in enumerate(sizes):
            payload = b"\x86" + struct.pack(">H", i) + bytes([i & 0xFF] * (size - 3))
            await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        received_seqs: list[int] = []

        async def _collect():
            while len(received_seqs) < len(sizes):
                data = await received.get()
                seq = struct.unpack(">H", data[1:3])[0]
                received_seqs.append(seq)

        await asyncio.wait_for(_collect(), timeout=10.0)
        assert received_seqs == list(range(len(sizes)))
