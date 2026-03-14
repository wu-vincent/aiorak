"""Port of MessageSizeTest.cpp - send messages of various sizes and verify data integrity."""

import math

import pytest

import aiorak
from tests.conftest import collect_packets

pytestmark = pytest.mark.asyncio

_ID_MARKER = 0x86


def _make_message(stride: int, index: int) -> bytes:
    """Build a message of *stride* bytes: first byte is the ID marker, rest are (index % 256)."""
    fill = index % 256
    return bytes([_ID_MARKER]) + bytes([fill] * (stride - 1))


@pytest.mark.parametrize("stride", [1, 10, 100, 500, 999, 1500, 1999])
async def test_message_size(server_factory, client_factory, stride: int):
    """Send ceil(4000/stride) messages of *stride* bytes and verify echoed data."""
    count = math.ceil(4000 / stride)

    # Server uses the default echo handler (no handler argument needed).
    server = await server_factory()
    addr = server.local_address
    client = await client_factory(addr)

    # Build and send all messages.
    expected: list[bytes] = []
    for i in range(count):
        msg = _make_message(stride, i)
        expected.append(msg)
        client.send(msg, reliability=aiorak.Reliability.RELIABLE_ORDERED)

    # Collect echoed messages.
    received = await collect_packets(client, count, timeout=5.0)

    assert len(received) == count
    for i, (got, want) in enumerate(zip(received, expected)):
        assert got == want, (
            f"Mismatch at message {i}: "
            f"len got={len(got)} want={len(want)}, "
            f"first bytes got={got[:4]!r} want={want[:4]!r}"
        )
