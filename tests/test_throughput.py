"""Port of LoopbackPerformanceTest.cpp – throughput measurement via echo server."""

import asyncio
import struct
import time

import pytest

import aiorak
from tests.conftest import collect_packets

pytestmark = pytest.mark.asyncio

PACKET_COUNT = 500
PACKET_SIZE = 400
SEND_RATE = 100  # packets per second
SEND_INTERVAL = 1.0 / SEND_RATE
DURATION = 3.0  # seconds to run sender


async def _send_packets(client: aiorak.Client, reliability: aiorak.Reliability):
    """Send PACKET_COUNT packets at ~SEND_RATE packets/sec."""
    sent = 0
    deadline = asyncio.get_event_loop().time() + DURATION
    while sent < PACKET_COUNT and asyncio.get_event_loop().time() < deadline:
        payload = struct.pack(">I", sent) + b"\xcc" * (PACKET_SIZE - 4)
        client.send(payload, reliability=reliability)
        sent += 1
        await asyncio.sleep(SEND_INTERVAL)
    return sent


@pytest.mark.slow
async def test_throughput_reliable_ordered(server_factory, client_factory):
    """Send 500 packets of 400 bytes with RELIABLE_ORDERED via echo server.
    All packets must be echoed back."""
    server = await server_factory()
    client = await client_factory(server.address)

    t_start = time.monotonic()
    sent = await _send_packets(client, aiorak.Reliability.RELIABLE_ORDERED)
    responses = await collect_packets(client, sent, timeout=DURATION + 10.0)
    t_elapsed = time.monotonic() - t_start

    assert len(responses) == sent

    # Verify ordering.
    for i, data in enumerate(responses):
        assert len(data) == PACKET_SIZE
        (idx,) = struct.unpack(">I", data[:4])
        assert idx == i, f"Out-of-order at position {i}: got index {idx}"

    pps = len(responses) / t_elapsed
    bps = len(responses) * PACKET_SIZE / t_elapsed
    print(
        f"\nRELIABLE_ORDERED throughput: {pps:.0f} pkt/s, "
        f"{bps / 1024:.1f} KB/s ({len(responses)} packets in {t_elapsed:.2f}s)"
    )


@pytest.mark.slow
async def test_throughput_reliable(server_factory, client_factory):
    """Send 500 packets of 400 bytes with RELIABLE via echo server.
    All packets must be echoed back (order not guaranteed)."""
    server = await server_factory()
    client = await client_factory(server.address)

    t_start = time.monotonic()
    sent = await _send_packets(client, aiorak.Reliability.RELIABLE)
    responses = await collect_packets(client, sent, timeout=DURATION + 10.0)
    t_elapsed = time.monotonic() - t_start

    assert len(responses) == sent

    for data in responses:
        assert len(data) == PACKET_SIZE

    pps = len(responses) / t_elapsed
    bps = len(responses) * PACKET_SIZE / t_elapsed
    print(
        f"\nRELIABLE throughput: {pps:.0f} pkt/s, {bps / 1024:.1f} KB/s ({len(responses)} packets in {t_elapsed:.2f}s)"
    )


@pytest.mark.slow
async def test_throughput_unreliable(server_factory, client_factory):
    """Send 500 packets of 400 bytes with UNRELIABLE via echo server.
    At least 50% must arrive (loopback should be nearly lossless)."""
    server = await server_factory()
    client = await client_factory(server.address)

    t_start = time.monotonic()
    sent = await _send_packets(client, aiorak.Reliability.UNRELIABLE)

    # For unreliable, we cannot know the exact count ahead of time.
    # Collect what we can within a reasonable window.
    received: list[bytes] = []

    async def _gather():
        async for data in client:
            received.append(data)
            if len(received) >= sent:
                return

    try:
        await asyncio.wait_for(_gather(), timeout=DURATION + 5.0)
    except TimeoutError:
        pass  # Unreliable may not deliver all packets.

    t_elapsed = time.monotonic() - t_start

    min_expected = sent // 2
    assert len(received) >= min_expected, f"UNRELIABLE: expected at least {min_expected} packets, got {len(received)}"

    for data in received:
        assert len(data) == PACKET_SIZE

    pps = len(received) / t_elapsed if t_elapsed > 0 else 0
    bps = len(received) * PACKET_SIZE / t_elapsed if t_elapsed > 0 else 0
    loss = (1 - len(received) / sent) * 100 if sent > 0 else 0
    print(
        f"\nUNRELIABLE throughput: {pps:.0f} pkt/s, "
        f"{bps / 1024:.1f} KB/s ({len(received)}/{sent} packets, "
        f"{loss:.1f}% loss, {t_elapsed:.2f}s)"
    )
