"""Integration tests adapted from ReliableOrderedTest.cpp — multi-channel ordering."""

from __future__ import annotations

import asyncio
import struct

import pytest

import aiorak
from aiorak import EventType, Reliability

from .conftest import collect_events


class TestReliableOrdered:
    async def test_packets_arrive_in_order_single_channel(self, server_and_client):
        """20 packets on channel 0, verify ordering."""
        server, client, _ = server_and_client
        srv_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)
        client_addr = srv_events[0].address

        num_packets = 20
        for i in range(num_packets):
            payload = b"\x86" + struct.pack(">I", i)
            await client.send(payload, reliability=Reliability.RELIABLE_ORDERED, channel=0)

        received: list[int] = []

        async def _collect():
            async for event in server:
                if event.type == EventType.RECEIVE:
                    seq = struct.unpack(">I", event.data[1:])[0]
                    received.append(seq)
                    if len(received) >= num_packets:
                        return

        await asyncio.wait_for(_collect(), timeout=10.0)
        assert received == list(range(num_packets))

    async def test_packets_arrive_in_order_multi_channel(self, server_factory, client_factory):
        """4 channels x 10 packets, verify per-channel ordering."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)

        await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        num_channels = 4
        num_packets = 10

        for ch in range(num_channels):
            for i in range(num_packets):
                payload = b"\x86" + struct.pack(">BH", ch, i)
                await client.send(payload, reliability=Reliability.RELIABLE_ORDERED, channel=ch)

        per_channel: dict[int, list[int]] = {ch: [] for ch in range(num_channels)}

        async def _collect():
            total = num_channels * num_packets
            count = 0
            async for event in server:
                if event.type == EventType.RECEIVE:
                    ch, seq = struct.unpack(">BH", event.data[1:])
                    per_channel[ch].append(seq)
                    count += 1
                    if count >= total:
                        return

        await asyncio.wait_for(_collect(), timeout=10.0)

        for ch in range(num_channels):
            assert per_channel[ch] == list(range(num_packets)), f"Channel {ch} out of order"

    async def test_variable_size_ordering(self, server_and_client):
        """Packets with sizes 10-2000 bytes, verify ordering preserved."""
        server, client, _ = server_and_client
        srv_events = await collect_events(server, EventType.CONNECT, count=1, timeout=3.0)

        sizes = [10, 50, 100, 500, 1000, 1500, 2000]
        for i, size in enumerate(sizes):
            # First byte is user message ID, next 2 are sequence, rest is padding
            payload = b"\x86" + struct.pack(">H", i) + bytes([i & 0xFF] * (size - 3))
            await client.send(payload, reliability=Reliability.RELIABLE_ORDERED)

        received_seqs: list[int] = []

        async def _collect():
            async for event in server:
                if event.type == EventType.RECEIVE:
                    seq = struct.unpack(">H", event.data[1:3])[0]
                    received_seqs.append(seq)
                    if len(received_seqs) >= len(sizes):
                        return

        await asyncio.wait_for(_collect(), timeout=10.0)
        assert received_seqs == list(range(len(sizes)))
