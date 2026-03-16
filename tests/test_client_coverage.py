"""Tests targeting uncovered branches in _client.py."""

import pytest

import aiorak

pytestmark = pytest.mark.asyncio


@pytest.mark.timeout(10)
async def test_client_connect_timeout():
    """Connecting to a non-listening port raises RakNetTimeoutError."""
    with pytest.raises(aiorak.RakNetTimeoutError):
        await aiorak.connect(("127.0.0.1", 19), timeout=1.0)


@pytest.mark.timeout(10)
async def test_client_mtu_property(server_and_client):
    """client.mtu returns the negotiated MTU after connection."""
    server, client, addr = server_and_client
    mtu = client.mtu
    assert isinstance(mtu, int)
    assert mtu > 0


@pytest.mark.timeout(10)
async def test_client_repr(server_and_client):
    """Client.__repr__ includes remote address and MTU."""
    server, client, addr = server_and_client
    r = repr(client)
    assert "Client" in r
    assert "remote_address" in r
    assert "mtu" in r


@pytest.mark.timeout(10)
async def test_client_context_manager(server_factory):
    """async with on Client calls close() on exit."""
    server = await server_factory()
    addr = server.address

    async with await aiorak.connect(addr, timeout=5.0) as client:
        assert client.is_connected

    # After exiting, client should be closed
    assert client._closed


@pytest.mark.timeout(10)
async def test_client_properties(server_and_client):
    """Test client.address, client.remote_address, client.guid, client.timeout."""
    server, client, addr = server_and_client
    assert isinstance(client.address, tuple)
    assert isinstance(client.remote_address, tuple)
    assert isinstance(client.guid, int)

    # timeout property
    original = client.timeout
    client.timeout = 30.0
    assert client.timeout == 30.0
    client.timeout = original
