"""Integration tests for the Client API: context manager, properties, disconnect."""

import asyncio

import pytest

import aiorak
from aiorak._connection import ConnectionState

pytestmark = pytest.mark.asyncio


class TestClientProperties:
    @pytest.mark.timeout(10)
    async def test_client_mtu_property(self, server_and_client):
        """client.mtu returns a positive integer after connection."""
        server, client, addr = server_and_client
        mtu = client.mtu
        assert isinstance(mtu, int)
        assert mtu > 0

    @pytest.mark.timeout(10)
    async def test_client_repr(self, server_and_client):
        """Client.__repr__ includes 'Client', 'remote_address', and 'mtu'."""
        server, client, addr = server_and_client
        r = repr(client)
        assert "Client" in r
        assert "remote_address" in r
        assert "mtu" in r

    @pytest.mark.timeout(10)
    async def test_client_properties(self, server_and_client):
        """client.address, remote_address, guid, and timeout properties work correctly."""
        server, client, addr = server_and_client
        assert isinstance(client.address, tuple)
        assert isinstance(client.remote_address, tuple)
        assert isinstance(client.guid, int)

        original = client.timeout
        client.timeout = 30.0
        assert client.timeout == 30.0
        client.timeout = original


class TestClientContextManager:
    @pytest.mark.timeout(10)
    async def test_client_context_manager(self, server_factory):
        """async with on Client calls close() on exit."""
        server = await server_factory()
        addr = server.address

        async with await aiorak.connect(addr, timeout=5.0) as client:
            assert client.is_connected

        assert client._closed


class TestClientDisconnect:
    @pytest.mark.timeout(10)
    async def test_client_close_cancelled_task(self, server_factory, client_factory):
        """Closing client while update task is running completes cleanly."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        await client.close()
        assert client._closed

    @pytest.mark.timeout(10)
    async def test_client_disconnect_signal(self, server_factory, client_factory):
        """Server disconnect causes client to detect connection loss."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        for conn in srv.connections:
            srv.disconnect(conn)

        await asyncio.sleep(0.5)
        assert client._closed or client._connection.state != ConnectionState.CONNECTED
