"""Port of FlowControlTest.cpp – relay server forwards data between clients."""

import asyncio
import struct

import pytest

import aiorak
from tests.conftest import wait_for_peers

pytestmark = pytest.mark.asyncio


def _make_relay_handler():
    """Create a relay handler that forwards packets to all other connections."""
    connections: dict[tuple[str, int], aiorak.Connection] = {}

    async def relay_handler(conn: aiorak.Connection):
        connections[conn.address] = conn
        try:
            async for data in conn:
                for addr, other in list(connections.items()):
                    if addr != conn.address:
                        await other.send(data)
        finally:
            connections.pop(conn.address, None)

    return relay_handler


async def test_relayed_data_arrives(server_factory, client_factory):
    """Client A sends 64-byte packets every 128ms for 3s through a relay
    server.  Client B must receive every packet."""
    relay_handler = _make_relay_handler()
    server = await server_factory(handler=relay_handler)
    addr = server.local_address

    sender = await client_factory(addr)
    receiver = await client_factory(addr)
    await wait_for_peers(server, 2)

    packet_size = 64
    interval = 0.128  # 128ms
    duration = 3.0

    send_count = 0
    received: list[bytes] = []

    async def _send():
        nonlocal send_count
        deadline = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < deadline:
            payload = struct.pack(">I", send_count) + b"\x00" * (packet_size - 4)
            await sender.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)
            send_count += 1
            await asyncio.sleep(interval)

    async def _recv():
        async for data in receiver:
            received.append(data)
            if len(received) >= send_count and send_count > 0:
                # Give a small grace period for any trailing packets.
                await asyncio.sleep(0.3)
                return

    # Run sender, then collect.
    await _send()
    # Allow time for relay delivery.
    await asyncio.wait_for(_recv(), timeout=duration + 5.0)

    assert send_count > 0, "Should have sent at least one packet"
    assert len(received) == send_count, (
        f"Expected {send_count} relayed packets, got {len(received)}"
    )


async def test_variable_packet_sizes(server_factory, client_factory):
    """For each size in [64, 256, 1024, 4096], send packets through relay at
    64ms intervals for 500ms.  Verify all packets arrive at the correct size."""
    relay_handler = _make_relay_handler()
    server = await server_factory(handler=relay_handler)
    addr = server.local_address

    sender = await client_factory(addr)
    receiver = await client_factory(addr)
    await wait_for_peers(server, 2)

    sizes = [64, 256, 1024, 4096]
    interval = 0.064  # 64ms
    duration_per_size = 0.5

    for packet_size in sizes:
        send_count = 0
        received: list[bytes] = []

        async def _send():
            nonlocal send_count
            deadline = asyncio.get_event_loop().time() + duration_per_size
            while asyncio.get_event_loop().time() < deadline:
                payload = struct.pack(">HI", packet_size, send_count) + b"\xAB" * (
                    packet_size - 6
                )
                await sender.send(
                    payload, reliability=aiorak.Reliability.RELIABLE_ORDERED
                )
                send_count += 1
                await asyncio.sleep(interval)

        async def _recv():
            async for data in receiver:
                received.append(data)
                if len(received) >= send_count and send_count > 0:
                    await asyncio.sleep(0.3)
                    return

        await _send()
        await asyncio.wait_for(_recv(), timeout=duration_per_size + 5.0)

        assert send_count > 0, f"Should have sent packets for size {packet_size}"
        assert len(received) == send_count, (
            f"Size {packet_size}: expected {send_count} packets, got {len(received)}"
        )
        for i, data in enumerate(received):
            assert len(data) == packet_size, (
                f"Size {packet_size}, packet {i}: expected {packet_size} bytes, "
                f"got {len(data)}"
            )
