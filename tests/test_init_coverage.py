"""Tests targeting uncovered branches in __init__.py."""

import pytest

import aiorak

pytestmark = pytest.mark.asyncio


@pytest.mark.timeout(10)
async def test_create_server_optional_params():
    """Pass all optional params to create_server() to cover kwarg branches."""

    async def handler(conn):
        async for _ in conn:
            pass

    server = await aiorak.create_server(
        ("127.0.0.1", 0),
        handler,
        max_connections=8,
        guid=12345,
        protocol_version=11,
        max_mtu=1400,
        min_mtu=400,
        num_internal_ids=20,
        timeout=15.0,
        rate_limit_ips=True,
    )
    try:
        assert server.guid == 12345
        assert server._protocol_version == 11
        assert server._max_mtu == 1400
        assert server._min_mtu == 400
        assert server._num_internal_ids == 20
        assert server._timeout == 15.0
        assert server._rate_limit_ips is True
    finally:
        await server.close()


@pytest.mark.timeout(10)
async def test_connect_optional_params(server_factory):
    """Pass all optional params to connect() to cover kwarg branches."""

    async def handler(conn):
        async for _ in conn:
            pass

    server = await server_factory(handler=handler)
    addr = server.address

    client = await aiorak.connect(
        addr,
        timeout=5.0,
        guid=67890,
        max_mtu=1400,
        min_mtu=400,
        mtu_discovery_sizes=(1400, 1200, 576),
        num_internal_ids=10,
    )
    try:
        assert client.is_connected
        assert client.guid == 67890
    finally:
        await client.close()


@pytest.mark.timeout(10)
async def test_ping_basic(server_factory):
    """Basic ping/pong to running server covers the ping() function."""
    server = await server_factory()
    server.set_offline_ping_response(b"test data")
    addr = server.address

    resp = await aiorak.ping(addr, timeout=3.0)
    assert resp.latency_ms >= 0
    assert resp.server_guid == server.guid
    assert resp.data == b"test data"


@pytest.mark.timeout(10)
async def test_ping_only_if_open(server_factory):
    """ping(only_if_open=True) uses ID_UNCONNECTED_PING_OPEN_CONNECTIONS."""
    server = await server_factory()
    addr = server.address

    resp = await aiorak.ping(addr, timeout=3.0, only_if_open=True)
    assert resp.latency_ms >= 0
    assert resp.server_guid == server.guid
