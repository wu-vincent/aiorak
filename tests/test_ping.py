"""Integration tests for ping/pong and offline messages."""

import asyncio

import pytest

import aiorak
from aiorak._bitstream import BitStream
from aiorak._constants import ID_UNCONNECTED_PONG, OFFLINE_MAGIC
from tests.conftest import wait_for_peers

pytestmark = pytest.mark.asyncio

CUSTOM_PONG_DATA = b"aiorak ping test response"


class TestPingBasic:
    @pytest.mark.timeout(10)
    async def test_ping_basic(self, server_factory):
        """Ping returns a PingResponse with valid latency, GUID, and custom data."""
        server = await server_factory()
        server.set_offline_ping_response(CUSTOM_PONG_DATA)
        addr = server.address

        response = await aiorak.ping(addr, timeout=3.0)

        assert response is not None
        assert response.latency_ms >= 0
        assert response.latency_ms < 50
        assert response.server_guid is not None and response.server_guid != 0
        assert response.data == CUSTOM_PONG_DATA

    @pytest.mark.timeout(10)
    async def test_ping_multiple_sequential(self, server_factory):
        """Five sequential pings all succeed with consistent low latencies."""
        server = await server_factory()
        server.set_offline_ping_response(CUSTOM_PONG_DATA)
        addr = server.address

        latencies = []
        for _ in range(5):
            response = await aiorak.ping(addr, timeout=3.0)
            assert response is not None
            assert response.data == CUSTOM_PONG_DATA
            assert response.latency_ms >= 0
            latencies.append(response.latency_ms)

        for i, lat in enumerate(latencies):
            assert lat < 50, f"Ping {i} latency {lat} ms exceeds 50 ms on localhost"

    @pytest.mark.timeout(10)
    async def test_ping_timeout(self):
        """Pinging a non-listening address raises RakNetTimeoutError."""
        with pytest.raises(aiorak.RakNetTimeoutError):
            await aiorak.ping(("127.0.0.1", 19), timeout=1.0)


class TestPingCapacity:
    @pytest.mark.timeout(10)
    async def test_ping_only_if_open_full_server(self, server_factory, client_factory):
        """Pinging a full server with only_if_open=True raises RakNetTimeoutError."""
        server = await server_factory(max_connections=1)
        server.set_offline_ping_response(CUSTOM_PONG_DATA)
        addr = server.address

        await client_factory(addr)
        await wait_for_peers(server, 1)

        with pytest.raises(aiorak.RakNetTimeoutError):
            await aiorak.ping(addr, timeout=2.0, only_if_open=True)

    @pytest.mark.timeout(10)
    async def test_ping_regardless_of_capacity(self, server_factory, client_factory):
        """Pinging a full server with only_if_open=False still returns a response."""
        server = await server_factory(max_connections=1)
        server.set_offline_ping_response(CUSTOM_PONG_DATA)
        addr = server.address

        await client_factory(addr)
        await wait_for_peers(server, 1)

        response = await aiorak.ping(addr, timeout=3.0, only_if_open=False)
        assert response is not None
        assert response.data == CUSTOM_PONG_DATA

    @pytest.mark.timeout(10)
    async def test_ping_open_connections_with_capacity(self, server_factory):
        """Pinging with only_if_open=True when server has capacity responds normally."""
        server = await server_factory()
        addr = server.address

        resp = await aiorak.ping(addr, timeout=3.0, only_if_open=True)
        assert resp.latency_ms >= 0
        assert resp.server_guid == server.guid


class TestPingEdgeCases:
    @pytest.mark.timeout(10)
    async def test_ping_bad_magic_in_pong(self):
        """Pong with bad magic is ignored, causing RakNetTimeoutError."""
        loop = asyncio.get_running_loop()

        class _FakeProto(asyncio.DatagramProtocol):
            def __init__(self):
                self._transport = None

            def connection_made(self, transport):
                self._transport = transport

            def datagram_received(self, data, addr):
                bs = BitStream()
                bs.write_uint8(ID_UNCONNECTED_PONG)
                bs.write_int64(0)
                bs.write_uint64(0xBEEF)
                bs.write_bytes(b"\x00" * 16)  # bad magic
                self._transport.sendto(bs.get_data(), addr)

        transport, proto = await loop.create_datagram_endpoint(
            _FakeProto,
            local_addr=("127.0.0.1", 0),
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            with pytest.raises(aiorak.RakNetTimeoutError):
                await aiorak.ping(("127.0.0.1", port), timeout=0.5)
        finally:
            transport.close()

    @pytest.mark.timeout(10)
    async def test_ping_non_pong_packet(self):
        """Garbage packet is ignored; subsequent valid pong is still received."""
        loop = asyncio.get_running_loop()

        class _FakeProto(asyncio.DatagramProtocol):
            def __init__(self):
                self._transport = None

            def connection_made(self, transport):
                self._transport = transport

            def datagram_received(self, data, addr):
                self._transport.sendto(b"\x00" * 5, addr)  # garbage
                bs = BitStream()
                bs.write_uint8(ID_UNCONNECTED_PONG)
                bs.write_int64(0)
                bs.write_uint64(0xCAFE)
                bs.write_bytes(OFFLINE_MAGIC)
                self._transport.sendto(bs.get_data(), addr)

        transport, proto = await loop.create_datagram_endpoint(
            _FakeProto,
            local_addr=("127.0.0.1", 0),
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            resp = await aiorak.ping(("127.0.0.1", port), timeout=3.0)
            assert resp.server_guid == 0xCAFE
        finally:
            transport.close()
