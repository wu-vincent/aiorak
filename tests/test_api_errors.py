"""Error paths and edge cases in the public Client/Server APIs."""

from __future__ import annotations

import asyncio

import pytest

import aiorak
from aiorak import Client, EventType, Reliability, Server

from .conftest import collect_events


class TestClientErrors:
    async def test_send_before_connect_raises_runtime_error(self):
        """Client() without connect(), send() should raise RuntimeError."""
        client = Client(("127.0.0.1", 9999))
        with pytest.raises(RuntimeError):
            await client.send(b"\x86hello")

    async def test_send_after_close_raises(self, server_factory, client_factory):
        """connect, close, send() should raise RuntimeError."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)
        assert client.is_connected

        await client.close()

        with pytest.raises(RuntimeError):
            await client.send(b"\x86hello")

    async def test_double_close_is_safe(self, server_factory, client_factory):
        """close() twice should not raise any exception."""
        server = await server_factory()
        addr = server.local_address
        client = await client_factory(addr)

        await client.close()
        await client.close()  # Should not raise

    async def test_is_connected_false_initially(self):
        """Client() should have is_connected == False."""
        client = Client(("127.0.0.1", 9999))
        assert client.is_connected is False

    async def test_local_address_before_connect(self):
        """local_address before connect should return ("0.0.0.0", 0)."""
        client = Client(("127.0.0.1", 9999))
        assert client.local_address == ("0.0.0.0", 0)


class TestServerErrors:
    async def test_send_to_unknown_address_raises_key_error(self, server_factory):
        """server.send(unknown_addr) should raise KeyError."""
        server = await server_factory()
        with pytest.raises(KeyError):
            await server.send(("192.168.255.255", 12345), b"\x86hello")

    async def test_double_close_is_safe(self, server_factory):
        """close() twice should not raise any exception."""
        server = await server_factory()
        await server.close()
        await server.close()  # Should not raise


class TestConnectErrors:
    async def test_connect_to_non_listening_port_times_out(self):
        """connect(port=1) should raise TimeoutError."""
        with pytest.raises(asyncio.TimeoutError):
            await aiorak.connect(("127.0.0.1", 1), timeout=1.0)
