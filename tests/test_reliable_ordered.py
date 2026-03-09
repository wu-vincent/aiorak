"""Port of ReliableOrderedTest.cpp – 32 channels x 50 packets, verify per-channel ordering."""

import asyncio
import os
import struct
from collections import defaultdict

import pytest

import aiorak
from tests.conftest import collect_packets

pytestmark = pytest.mark.asyncio

NUM_CHANNELS = 32
PACKETS_PER_CHANNEL = 50
TOTAL_PACKETS = NUM_CHANNELS * PACKETS_PER_CHANNEL


async def test_reliable_ordered_multi_channel(server_factory, client_factory):
    """Send packets on 32 channels with RELIABLE_ORDERED and verify that
    per-channel ordering is preserved after the server echoes them back."""
    server = await server_factory()
    client = await client_factory(server.local_address)

    # Send packets: 50 rounds, each round sends one packet on every channel.
    for seq in range(PACKETS_PER_CHANNEL):
        for ch in range(NUM_CHANNELS):
            # Header: 0x86 (ID) | seq (4 bytes) | channel (1 byte) | random padding
            padding_len = int.from_bytes(os.urandom(2), "big") % 5000 + 1
            payload = (
                struct.pack(">BIB", 0x86, seq, ch)
                + os.urandom(padding_len)
            )
            await client.send(
                payload,
                reliability=aiorak.Reliability.RELIABLE_ORDERED,
                channel=ch,
            )
        # Pace at ~30 ms between rounds to avoid overwhelming the link.
        await asyncio.sleep(0.03)

    # Collect all echoed packets.
    responses = await collect_packets(client, TOTAL_PACKETS, timeout=15.0)

    assert len(responses) == TOTAL_PACKETS

    # Group responses by channel and verify per-channel ordering.
    per_channel: dict[int, list[int]] = defaultdict(list)
    for data in responses:
        assert len(data) >= 6, "Response too short to contain header fields"
        _, seq, ch = struct.unpack(">BIB", data[:6])
        per_channel[ch].append(seq)

    # Every channel must have received exactly PACKETS_PER_CHANNEL packets.
    for ch in range(NUM_CHANNELS):
        received = per_channel[ch]
        assert len(received) == PACKETS_PER_CHANNEL, (
            f"Channel {ch}: expected {PACKETS_PER_CHANNEL} packets, got {len(received)}"
        )
        # Verify strictly ascending order within the channel.
        for i in range(1, len(received)):
            assert received[i] > received[i - 1], (
                f"Channel {ch}: out-of-order at position {i} "
                f"(seq {received[i]} <= {received[i - 1]})"
            )
