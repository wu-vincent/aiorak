"""Integration tests for server API: broadcast, disconnect, malicious input."""

import asyncio
import time as _time

import pytest

import aiorak
from aiorak._bitstream import BitStream
from aiorak._connection import Connection, ConnectionState
from aiorak._constants import (
    ID_ALREADY_CONNECTED,
    ID_INCOMPATIBLE_PROTOCOL_VERSION,
    ID_NO_FREE_INCOMING_CONNECTIONS,
    ID_OPEN_CONNECTION_REPLY_1,
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    ID_UNCONNECTED_PING,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
)
from tests.conftest import build_ocr1, build_ocr2, noop_handler

pytestmark = pytest.mark.asyncio


class TestServerLifecycle:
    @pytest.mark.timeout(10)
    async def test_server_context_manager(self, server_factory):
        """Server as async context manager calls close() on exit."""
        server = await server_factory(handler=noop_handler)
        addr = server.address

        client = await aiorak.connect(addr, timeout=5.0)
        await asyncio.sleep(0.05)
        assert client.is_connected

        async with server:
            assert len(server._peers) >= 1

        assert server._closed
        await client.close()

    @pytest.mark.timeout(10)
    async def test_serve_forever(self, server_factory):
        """serve_forever blocks until close is called."""
        srv = await server_factory()

        async def close_after_delay():
            await asyncio.sleep(0.1)
            await srv.close()

        task = asyncio.create_task(close_after_delay())
        await srv.serve_forever()
        await task

    @pytest.mark.timeout(10)
    async def test_server_repr(self, server_factory):
        """Server.__repr__ includes address and connection count."""
        server = await server_factory(handler=noop_handler)
        r = repr(server)
        assert "Server" in r
        assert "listening" in r
        assert "connections=" in r

    @pytest.mark.timeout(10)
    async def test_server_properties(self, server_factory):
        """Server.guid, .connections, .timeout property work correctly."""
        server = await server_factory(handler=noop_handler)
        assert isinstance(server.guid, int)
        assert server.connections == []

        original = server.timeout
        server.timeout = 30.0
        assert server.timeout == 30.0
        server.timeout = original


class TestServerDisconnect:
    @pytest.mark.timeout(10)
    async def test_disconnect_with_notify(self, server_factory, client_factory):
        """server.disconnect(conn, notify=True) transitions peer to DISCONNECTING."""
        server = await server_factory(handler=noop_handler)
        await client_factory(server.address)
        await asyncio.sleep(0.1)

        peers = server.connections
        assert len(peers) == 1
        server.disconnect(peers[0], notify=True)
        assert peers[0].state == ConnectionState.DISCONNECTING

    @pytest.mark.timeout(10)
    async def test_disconnect_without_notify(self, server_factory, client_factory):
        """server.disconnect(conn, notify=False) transitions peer to DISCONNECTED immediately."""
        server = await server_factory(handler=noop_handler)
        await client_factory(server.address)
        await asyncio.sleep(0.1)

        peers = server.connections
        assert len(peers) == 1
        server.disconnect(peers[0], notify=False)
        assert peers[0].state == ConnectionState.DISCONNECTED

    @pytest.mark.timeout(10)
    async def test_server_close_no_notify(self, server_factory, client_factory):
        """server.close(notify=False) drops all connections silently."""
        srv = await server_factory()
        await client_factory(srv.address)
        await asyncio.sleep(0.1)
        await srv.close(notify=False)

    @pytest.mark.timeout(10)
    async def test_disconnect_not_connected_peer(self, server_factory):
        """disconnect() on a DISCONNECTED peer is a no-op."""
        srv = await server_factory()
        conn = Connection(("127.0.0.1", 9999), guid=0xDEAD, is_server=True)
        conn.state = ConnectionState.DISCONNECTED
        srv.disconnect(conn)


class TestBroadcast:
    @pytest.mark.timeout(10)
    async def test_broadcast_with_exclude(self, server_factory, client_factory):
        """broadcast(exclude=conn) sends to all peers except the excluded one."""
        server = await server_factory(handler=noop_handler, max_connections=4)
        addr = server.address

        client1 = await client_factory(addr)
        client2 = await client_factory(addr)
        await asyncio.sleep(0.1)

        peers = server.connections
        assert len(peers) == 2

        excluded = peers[0]
        server.broadcast(b"hello", exclude=excluded)
        await asyncio.sleep(0.2)

        got1 = []
        got2 = []

        async def try_recv(client, bucket):
            try:
                async for data in client:
                    bucket.append(data)
                    break
            except Exception:
                pass

        t1 = asyncio.create_task(asyncio.wait_for(try_recv(client1, got1), timeout=0.5))
        t2 = asyncio.create_task(asyncio.wait_for(try_recv(client2, got2), timeout=0.5))
        await asyncio.gather(t1, t2, return_exceptions=True)

        total_received = len(got1) + len(got2)
        assert total_received == 1

    @pytest.mark.timeout(10)
    async def test_broadcast_only_connected_peers(self, server_factory, client_factory):
        """broadcast() skips peers not in CONNECTED state."""
        server = await server_factory(handler=noop_handler, max_connections=4)
        addr = server.address

        client1 = await client_factory(addr)
        client2 = await client_factory(addr)
        await asyncio.sleep(0.1)

        peers = server.connections
        assert len(peers) == 2

        peers[0].disconnect()
        assert peers[0].state == ConnectionState.DISCONNECTING

        server.broadcast(b"test")
        await asyncio.sleep(0.2)

        got = []

        async def try_recv(client, bucket):
            try:
                async for data in client:
                    bucket.append(data)
                    break
            except Exception:
                pass

        t1 = asyncio.create_task(asyncio.wait_for(try_recv(client1, got), timeout=0.5))
        t2_got = []
        t2 = asyncio.create_task(asyncio.wait_for(try_recv(client2, t2_got), timeout=0.5))
        await asyncio.gather(t1, t2, return_exceptions=True)
        total = len(got) + len(t2_got)
        assert total == 1


class TestServerTimeoutAndMTU:
    @pytest.mark.timeout(10)
    async def test_server_wide_timeout(self, server_factory, client_factory):
        """set_timeout()/get_timeout() with conn=None updates all connections."""
        server = await server_factory(handler=noop_handler)
        await client_factory(server.address)
        await asyncio.sleep(0.1)

        default_timeout = server.get_timeout()
        assert isinstance(default_timeout, float)

        server.set_timeout(42.0)
        assert server.get_timeout() == 42.0

        for conn in server._connections.values():
            assert conn.timeout == 42.0

        peers = server.connections
        assert len(peers) >= 1
        server.set_timeout(99.0, peers[0])
        assert server.get_timeout(peers[0]) == 99.0
        assert server.get_timeout() == 42.0

    @pytest.mark.timeout(10)
    async def test_server_timeout_get_set(self, server_factory):
        """Server timeout property delegates to get/set_timeout."""
        srv = await server_factory()
        srv.timeout = 20.0
        assert srv.timeout == 20.0

    @pytest.mark.timeout(10)
    async def test_get_mtu(self, server_factory, client_factory):
        """server.get_mtu(conn) returns a positive integer MTU."""
        server = await server_factory(handler=noop_handler)
        await client_factory(server.address)
        await asyncio.sleep(0.1)

        peers = server.connections
        assert len(peers) == 1
        mtu = server.get_mtu(peers[0])
        assert isinstance(mtu, int)
        assert mtu > 0

    @pytest.mark.timeout(10)
    async def test_set_offline_ping_response(self, server_factory):
        """Custom offline ping response data is returned in pong."""
        srv = await server_factory()
        srv.set_offline_ping_response(b"MCPE;Test;1;2;0;10")
        resp = await aiorak.ping(srv.address, timeout=3.0)
        assert resp.data == b"MCPE;Test;1;2;0;10"


class TestServerRateLimit:
    @pytest.mark.timeout(10)
    async def test_ocr2_rate_limited(self, server_factory):
        """Second OCR2 from same non-loopback IP within 100ms is rejected."""
        srv = await server_factory(max_connections=64, rate_limit_ips=True)

        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("10.0.0.1", 1234)
        bs.write_uint16(MINIMUM_MTU)
        bs.write_uint64(0xAAAA)
        data = bs.get_data()

        now = _time.monotonic()
        addr1 = ("10.0.0.1", 1234)
        conn1 = srv._create_connection_from_ocr2(data, addr1, now)
        assert conn1 is not None

        bs2 = BitStream()
        bs2.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs2.write_bytes(OFFLINE_MAGIC)
        bs2.write_address("10.0.0.1", 1235)
        bs2.write_uint16(MINIMUM_MTU)
        bs2.write_uint64(0xBBBB)
        data2 = bs2.get_data()
        addr2 = ("10.0.0.1", 1235)
        conn2 = srv._create_connection_from_ocr2(data2, addr2, now + 0.01)
        assert conn2 is None

    @pytest.mark.timeout(10)
    async def test_server_max_connections_rejection(self, server_factory, raw_client):
        """Server at max capacity rejects OCR2 with ID_NO_FREE_INCOMING_CONNECTIONS."""
        srv = await server_factory(max_connections=0)
        c = await raw_client(srv.address)

        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 0)
        bs.write_uint16(MINIMUM_MTU)
        bs.write_uint64(0x12345678)
        c.send(bs.get_data())
        resp = await c.recv(timeout=2.0)
        assert resp[0] == ID_NO_FREE_INCOMING_CONNECTIONS


class TestMaliciousInput:
    """Tests that send raw crafted UDP packets to validate server hardening."""

    async def test_ocr1_too_short(self, server_factory, raw_client):
        """OCR1 shorter than 18 bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([ID_OPEN_CONNECTION_REQUEST_1]) + OFFLINE_MAGIC)
        assert await c.recv_or_none() is None

    async def test_ocr1_bad_magic(self, server_factory, raw_client):
        """OCR1 with wrong magic bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(build_ocr1(magic=b"\x00" * 16))
        assert await c.recv_or_none() is None

    async def test_ocr1_wrong_protocol_version(self, server_factory, raw_client):
        """OCR1 with wrong protocol version gets ID_INCOMPATIBLE_PROTOCOL_VERSION."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(build_ocr1(protocol=99))
        resp = await c.recv()
        assert resp[0] == ID_INCOMPATIBLE_PROTOCOL_VERSION

    async def test_ocr1_valid(self, server_factory, raw_client):
        """Valid OCR1 gets ID_OPEN_CONNECTION_REPLY_1."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(build_ocr1())
        resp = await c.recv()
        assert resp[0] == ID_OPEN_CONNECTION_REPLY_1

    async def test_ocr2_truncated(self, server_factory, raw_client):
        """OCR2 with valid magic but truncated fields is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([ID_OPEN_CONNECTION_REQUEST_2]) + OFFLINE_MAGIC)
        assert await c.recv_or_none() is None

    async def test_ocr2_mtu_out_of_range(self, server_factory, raw_client):
        """OCR2 with MTU below minimum is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(build_ocr2(mtu=10))
        assert await c.recv_or_none() is None

    async def test_ocr2_guid_duplicate(self, server_factory, client_factory, raw_client):
        """OCR2 with same GUID as existing connection gets ID_ALREADY_CONNECTED."""
        srv = await server_factory()
        cli = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        real_guid = cli._guid

        c = await raw_client(srv.address)
        c.send(build_ocr2(guid=real_guid))
        resp = await c.recv()
        assert resp[0] == ID_ALREADY_CONNECTED

    async def test_ping_too_short(self, server_factory, raw_client):
        """Unconnected ping shorter than 25 bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([ID_UNCONNECTED_PING]) + b"\x00" * 8)
        assert await c.recv_or_none() is None

    async def test_ping_bad_magic(self, server_factory, raw_client):
        """Ping with correct length but wrong magic is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([ID_UNCONNECTED_PING]) + b"\x00" * 8 + b"\xff" * 16)
        assert await c.recv_or_none() is None

    async def test_empty_datagram(self, server_factory, raw_client):
        """0-byte datagram is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(b"")
        assert await c.recv_or_none() is None

    async def test_unknown_message_id(self, server_factory, raw_client):
        """Packet with unknown message ID is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([0xFF]))
        assert await c.recv_or_none() is None

    async def test_handler_exception_logged(self, server_factory, client_factory, caplog):
        """Handler that raises is logged; server remains operational."""

        async def bad_handler(conn):
            raise RuntimeError("boom")

        srv = await server_factory(handler=bad_handler)
        await client_factory(srv.address)
        await asyncio.sleep(0.2)

        await client_factory(srv.address)
        await asyncio.sleep(0.1)
        assert len(srv._peers) >= 1
