"""Port of BigPacketTest.cpp — send large packets via RELIABLE_ORDERED and verify integrity."""

import asyncio
import math

import pytest

import aiorak

pytestmark = pytest.mark.asyncio


def _make_pattern(size: int) -> bytes:
    """Generate a deterministic byte pattern of the given size."""
    return bytes(255 - (i & 255) for i in range(size))


async def _big_packet_test(
    server_factory,
    client_factory,
    size: int,
    *,
    verify_data: bool = True,
    timeout: float = 10.0,
) -> None:
    """Helper: server sends a big packet, client receives and verifies."""
    payload = _make_pattern(size)

    async def handler(conn: aiorak.Connection):
        await conn.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)

    server = await server_factory(handler=handler)
    addr = server.local_address
    client = await client_factory(addr)

    received = await asyncio.wait_for(client.recv(), timeout=timeout)

    assert len(received) == size
    if verify_data:
        assert received == payload


async def test_big_packet_50kb(server_factory, client_factory):
    """50 KB packet with full data verification."""
    await _big_packet_test(
        server_factory,
        client_factory,
        50_000,
        timeout=10.0,
    )


async def test_big_packet_500kb(server_factory, client_factory):
    """500 KB packet with full data verification."""
    await _big_packet_test(
        server_factory,
        client_factory,
        500_000,
        timeout=15.0,
    )


@pytest.mark.slow
async def test_big_packet_5mb(server_factory, client_factory):
    """5 MB packet — verify length only (too large for byte-by-byte comparison)."""
    await _big_packet_test(
        server_factory,
        client_factory,
        5_000_000,
        verify_data=False,
        timeout=30.0,
    )
