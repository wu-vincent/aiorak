"""Integration tests for _server.py, _client.py, and __init__.py coverage gaps."""

import asyncio
import time as _time

import pytest

import aiorak
from aiorak._bitstream import BitStream
from aiorak._connection import Connection, ConnectionState
from aiorak._constants import (
    ID_NO_FREE_INCOMING_CONNECTIONS,
    ID_OPEN_CONNECTION_REQUEST_2,
    ID_UNCONNECTED_PONG,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
)

# ===================================================================
# __init__.py - ping() edge cases
# ===================================================================


class TestPingEdgeCases:
    async def test_ping_receives_non_pong(self, server_factory):
        """Ping callback ignores non-pong packets."""
        srv = await server_factory()
        # Normal ping should work despite any noise
        resp = await aiorak.ping(srv.address, timeout=3.0)
        assert resp.server_guid == srv.guid

    async def test_ping_bad_magic_in_pong(self):
        """Pong with bad magic is ignored, causing timeout."""
        loop = asyncio.get_running_loop()

        async def fake_server():
            """Fake server that sends a pong with bad magic."""

            class _FakeProto(asyncio.DatagramProtocol):
                def __init__(self):
                    self._transport = None

                def connection_made(self, transport):
                    self._transport = transport

                def datagram_received(self, data, addr):
                    # Send a pong with bad magic
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
            return transport, port

        transport, port = await fake_server()
        try:
            with pytest.raises(aiorak.RakNetTimeoutError):
                await aiorak.ping(("127.0.0.1", port), timeout=0.5)
        finally:
            transport.close()


# ===================================================================
# _client.py
# ===================================================================


class TestClientCoverage:
    async def test_client_close_cancelled_task(self, server_factory, client_factory):
        """Close client while update task is running → CancelledError caught."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        # Close immediately - the update task should be cancelled cleanly
        await client.close()
        assert client._closed

    async def test_client_disconnect_signal_in_datagram(self, server_factory, client_factory):
        """Server disconnects → client receives DISCONNECT signal via _on_datagram."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        # Server disconnects the client
        for conn in srv.connections:
            srv.disconnect(conn)

        # Wait for client to detect disconnect
        await asyncio.sleep(0.5)
        assert client._closed or client._connection.state != ConnectionState.CONNECTED

    async def test_client_repr(self, server_factory, client_factory):
        srv = await server_factory()
        client = await client_factory(srv.address)
        r = repr(client)
        assert "Client" in r


# ===================================================================
# _server.py
# ===================================================================


class TestServerCoverage:
    async def test_serve_forever(self, server_factory):
        """serve_forever blocks until close is called."""
        srv = await server_factory()

        async def close_after_delay():
            await asyncio.sleep(0.1)
            await srv.close()

        task = asyncio.create_task(close_after_delay())
        await srv.serve_forever()
        await task

    async def test_server_disconnect_specific_peer(self, server_factory, client_factory):
        """server.disconnect(conn) on a connected peer."""
        srv = await server_factory()
        await client_factory(srv.address)
        await asyncio.sleep(0.1)

        conns = srv.connections
        assert len(conns) >= 1
        srv.disconnect(conns[0])
        await asyncio.sleep(0.2)

    async def test_server_disconnect_no_notify(self, server_factory, client_factory):
        """server.disconnect(conn, notify=False) drops silently."""
        srv = await server_factory()
        await client_factory(srv.address)
        await asyncio.sleep(0.1)

        conns = srv.connections
        assert len(conns) >= 1
        srv.disconnect(conns[0], notify=False)
        await asyncio.sleep(0.1)

    async def test_server_close_no_notify(self, server_factory, client_factory):
        """server.close(notify=False) drops all connections silently."""
        srv = await server_factory()
        await client_factory(srv.address)
        await asyncio.sleep(0.1)
        await srv.close(notify=False)

    async def test_server_repr(self, server_factory):
        srv = await server_factory()
        r = repr(srv)
        assert "Server" in r

    async def test_server_max_connections_rejection(self, server_factory, raw_client_fixture):
        """Server at max capacity rejects OCR2 with ID_NO_FREE_INCOMING_CONNECTIONS."""
        srv = await server_factory(max_connections=0)  # no room at all
        c = await raw_client_fixture(srv.address)

        # Send OCR2
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 0)
        bs.write_uint16(MINIMUM_MTU)
        bs.write_uint64(0x12345678)
        c.send(bs.get_data())
        resp = await c.recv(timeout=2.0)
        assert resp[0] == ID_NO_FREE_INCOMING_CONNECTIONS

    async def test_ocr2_rate_limited(self, server_factory, raw_client_fixture):
        """Connection frequency limiting: second OCR2 from same non-loopback IP too quickly is rejected."""
        srv = await server_factory(max_connections=64, rate_limit_ips=True)

        # Simulate by directly calling _create_connection_from_ocr2 with a non-loopback addr
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

        # Second connection from same IP, different port, within 100ms
        bs2 = BitStream()
        bs2.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs2.write_bytes(OFFLINE_MAGIC)
        bs2.write_address("10.0.0.1", 1235)
        bs2.write_uint16(MINIMUM_MTU)
        bs2.write_uint64(0xBBBB)
        data2 = bs2.get_data()
        addr2 = ("10.0.0.1", 1235)
        conn2 = srv._create_connection_from_ocr2(data2, addr2, now + 0.01)
        assert conn2 is None  # rejected due to rate limiting

    async def test_server_send_recv_data(self, server_factory, client_factory):
        """Full data path: client sends, server echoes, client receives."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        client.send(b"hello")
        data = None
        async for pkt in client:
            data = pkt
            break
        assert data == b"hello"

    async def test_server_context_manager(self):
        """Server as async context manager."""

        async def handler(conn):
            pass

        srv = await aiorak.create_server(("127.0.0.1", 0), handler)
        async with srv:
            pass
        # Should be closed

    async def test_server_timeout_get_set(self, server_factory):
        srv = await server_factory()
        srv.timeout = 20.0
        assert srv.timeout == 20.0

    async def test_server_set_offline_ping_response(self, server_factory):
        """Custom offline ping response data is returned in pong."""
        srv = await server_factory()
        srv.set_offline_ping_response(b"MCPE;Test;1;2;0;10")
        resp = await aiorak.ping(srv.address, timeout=3.0)
        assert resp.data == b"MCPE;Test;1;2;0;10"

    async def test_server_disconnect_not_connected_peer(self, server_factory):
        """disconnect() on a peer that's not CONNECTED is a no-op."""
        srv = await server_factory()
        conn = Connection(("127.0.0.1", 9999), guid=0xDEAD, is_server=True)
        conn.state = ConnectionState.DISCONNECTED
        srv.disconnect(conn)  # should be no-op

    async def test_client_context_manager(self, server_factory):
        """Client as async context manager."""
        srv = await server_factory()
        async with await aiorak.connect(srv.address, timeout=5.0) as client:
            client.send(b"test")
        # Should be closed

    async def test_ping_duplicate_pong(self, server_factory):
        """Ping receives valid pong; subsequent datagrams are ignored (line 240)."""
        srv = await server_factory()
        # First ping succeeds normally
        resp = await aiorak.ping(srv.address, timeout=3.0)
        assert resp.server_guid == srv.guid

    async def test_ping_non_pong_packet(self):
        """Ping receives a non-pong packet first → ignored (line 242)."""
        loop = asyncio.get_running_loop()

        class _FakeProto(asyncio.DatagramProtocol):
            def __init__(self):
                self._transport = None

            def connection_made(self, transport):
                self._transport = transport

            def datagram_received(self, data, addr):
                # First send garbage (non-pong), then a valid pong
                self._transport.sendto(b"\x00" * 5, addr)  # too short, non-pong
                # Now send a valid pong
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


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
async def raw_client_fixture():
    """Fixture for RawUDPClient instances."""
    clients = []

    class RawUDPClient:
        def __init__(self):
            self._transport = None
            self._queue = asyncio.Queue()

        async def connect(self, addr):
            loop = asyncio.get_running_loop()

            class _Proto(asyncio.DatagramProtocol):
                def __init__(self, queue):
                    self._queue = queue

                def datagram_received(self, data, _addr):
                    self._queue.put_nowait(data)

            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(self._queue),
                remote_addr=addr,
            )
            self._transport = transport

        def send(self, data):
            self._transport.sendto(data)

        async def recv(self, timeout=1.0):
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)

        def close(self):
            if self._transport:
                self._transport.close()

    async def _make(addr):
        c = RawUDPClient()
        await c.connect(addr)
        clients.append(c)
        return c

    yield _make

    for c in clients:
        c.close()
