"""Port of BurstTest.cpp – send a burst of messages and verify all arrive in order."""

import struct

import pytest

import aiorak
from tests.conftest import collect_packets

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "msg_size, msg_count",
    [
        (64, 128),
        (512, 64),
        (4096, 16),
    ],
    ids=["64B-x128", "512B-x64", "4KB-x16"],
)
async def test_burst_reliable_ordered(server_factory, client_factory, msg_size, msg_count):
    """Send *msg_count* messages of *msg_size* bytes in a burst and verify
    that all echoed responses arrive in the correct order."""
    server = await server_factory()
    client = await client_factory(server.address)

    # Build messages: 1-byte ID (0x86) + 4-byte big-endian index + zero padding.
    header_len = 1 + 4  # ID byte + index
    padding_len = msg_size - header_len
    assert padding_len >= 0, "msg_size must be >= 5"

    messages: list[bytes] = []
    for i in range(msg_count):
        payload = struct.pack(">BI", 0x86, i) + (b"\x00" * padding_len)
        messages.append(payload)

    # Send all messages as fast as possible (burst).
    for msg in messages:
        client.send(msg, reliability=aiorak.Reliability.RELIABLE_ORDERED)

    # Collect all echoed responses.
    responses = await collect_packets(client, msg_count, timeout=10.0)

    assert len(responses) == msg_count

    # Verify ordering: the embedded index in each response must be sequential.
    for expected_idx, data in enumerate(responses):
        assert len(data) == msg_size
        (received_idx,) = struct.unpack(">I", data[1:5])
        assert received_idx == expected_idx, f"Out-of-order at position {expected_idx}: got index {received_idx}"
