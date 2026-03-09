"""Port of PingTest.cpp — verify ping latency measurement and timeout behaviour."""

import asyncio

import pytest

import aiorak

pytestmark = pytest.mark.asyncio

CUSTOM_PONG_DATA = b"aiorak ping test response"


@pytest.mark.timeout(10)
async def test_offline_ping(server_factory):
    """Ping a server with a custom offline response.

    Verifies that latency is reasonable on localhost (< 50 ms), that the
    server GUID is set, and that the response data matches what was configured.
    """
    server = await server_factory()
    server.set_offline_ping_response(CUSTOM_PONG_DATA)
    addr = server.local_address

    response = await aiorak.ping(addr, timeout=3.0)

    assert response is not None
    assert response.latency_ms >= 0, "Latency must be non-negative"
    assert response.latency_ms < 50, (
        f"Localhost latency should be < 50 ms, got {response.latency_ms}"
    )
    assert response.server_guid is not None and response.server_guid != 0, (
        "Server GUID should be set"
    )
    assert response.data == CUSTOM_PONG_DATA


@pytest.mark.timeout(10)
async def test_multiple_pings(server_factory):
    """Ping the same server 5 times in sequence.

    All pings should return valid responses with consistent, low latencies
    on localhost.
    """
    server = await server_factory()
    server.set_offline_ping_response(CUSTOM_PONG_DATA)
    addr = server.local_address

    latencies = []
    for _ in range(5):
        response = await aiorak.ping(addr, timeout=3.0)
        assert response is not None
        assert response.data == CUSTOM_PONG_DATA
        assert response.latency_ms >= 0
        latencies.append(response.latency_ms)

    # All localhost pings should be well under 50 ms.
    for i, lat in enumerate(latencies):
        assert lat < 50, f"Ping {i} latency {lat} ms exceeds 50 ms on localhost"


@pytest.mark.timeout(10)
async def test_ping_timeout():
    """Pinging a non-existent address should raise TimeoutError."""
    # Use a port that is almost certainly not running a RakNet server.
    unreachable_addr = ("127.0.0.1", 19)

    with pytest.raises(TimeoutError):
        await aiorak.ping(unreachable_addr, timeout=1.0)
