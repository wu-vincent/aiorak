"""Tests for the offline (unconnected) ping/pong API."""


import asyncio

import pytest

import aiorak
from aiorak import PingResponse


class TestOfflinePing:
    async def test_basic_ping_pong(self, server_factory):
        """Server responds to ping, client gets PingResponse."""
        server = await server_factory()
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert isinstance(resp, PingResponse)

    async def test_ping_returns_latency(self, server_factory):
        """latency_ms should be positive and reasonable on loopback."""
        server = await server_factory()
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert resp.latency_ms > 0
        assert resp.latency_ms < 1000  # Loopback should be well under 1s

    async def test_ping_returns_server_guid(self, server_factory):
        """server_guid in response should match the server's actual GUID."""
        server = await server_factory()
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert resp.server_guid == server._guid

    async def test_ping_with_offline_data(self, server_factory):
        """Custom offline data set via set_offline_ping_response appears in response."""
        server = await server_factory()
        server.set_offline_ping_response(b"hello world")
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert resp.data == b"hello world"

    async def test_ping_empty_offline_data(self, server_factory):
        """PingResponse.data is empty by default."""
        server = await server_factory()
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert resp.data == b""

    async def test_ping_timeout(self):
        """Pinging a non-listening port should raise TimeoutError."""
        with pytest.raises(asyncio.TimeoutError):
            await aiorak.ping(("127.0.0.1", 1), timeout=1.0)

    async def test_ping_open_connections_only_full(self, server_factory, client_factory):
        """only_if_open=True with a full server should time out."""
        server = await server_factory(max_connections=1)
        addr = server.local_address

        # Fill the single slot
        _client = await client_factory(addr)

        with pytest.raises(asyncio.TimeoutError):
            await aiorak.ping(addr, timeout=1.0, only_if_open=True)

    async def test_ping_open_connections_available(self, server_factory):
        """only_if_open=True with open slots should get a response."""
        server = await server_factory(max_connections=10)
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0, only_if_open=True)
        assert isinstance(resp, PingResponse)

    async def test_multiple_pings(self, server_factory):
        """5 sequential pings should all succeed."""
        server = await server_factory()
        addr = server.local_address

        for _ in range(5):
            resp = await aiorak.ping(addr, timeout=3.0)
            assert resp.latency_ms > 0

    async def test_ping_does_not_create_connection(self, server_factory):
        """After pinging, the server should have no connections."""
        server = await server_factory()
        addr = server.local_address

        await aiorak.ping(addr, timeout=3.0)
        assert len(server._connections) == 0

    async def test_ping_address_matches(self, server_factory):
        """PingResponse.address should match the server address."""
        server = await server_factory()
        addr = server.local_address

        resp = await aiorak.ping(addr, timeout=3.0)
        assert resp.address[1] == addr[1]  # Port should match
