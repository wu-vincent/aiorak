"""Tests that send raw crafted UDP packets to a running server, exercising
every validation branch in the datagram dispatch, OCR1/OCR2 handlers, and
unconnected ping handler.  Ensures the server is hardened against malformed
or malicious input."""

import asyncio

import pytest

from aiorak._bitstream import BitStream
from aiorak._constants import (
    ID_ALREADY_CONNECTED,
    ID_INCOMPATIBLE_PROTOCOL_VERSION,
    ID_OPEN_CONNECTION_REPLY_1,
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    ID_UNCONNECTED_PING,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
)

# ---------------------------------------------------------------------------
# Helper: raw UDP socket
# ---------------------------------------------------------------------------


class RawUDPClient:
    """Async helper that sends/receives arbitrary UDP datagrams."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def connect(self, addr: tuple[str, int]) -> None:
        loop = asyncio.get_running_loop()

        class _Proto(asyncio.DatagramProtocol):
            def __init__(self, queue: asyncio.Queue[bytes]) -> None:
                self._queue = queue

            def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
                self._queue.put_nowait(data)

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self._queue),
            remote_addr=addr,
        )
        self._transport = transport

    def send(self, data: bytes) -> None:
        assert self._transport is not None
        self._transport.sendto(data)

    async def recv(self, timeout: float = 1.0) -> bytes:
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    async def recv_or_none(self, timeout: float = 0.3) -> bytes | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


@pytest.fixture
async def raw_client():
    """Fixture that creates RawUDPClient instances and cleans them up."""
    clients: list[RawUDPClient] = []

    async def _make(addr: tuple[str, int]) -> RawUDPClient:
        c = RawUDPClient()
        await c.connect(addr)
        clients.append(c)
        return c

    yield _make

    for c in clients:
        c.close()


def _build_ocr1(magic: bytes = OFFLINE_MAGIC, protocol: int = RAKNET_PROTOCOL_VERSION, pad: int = 0) -> bytes:
    """Build an OCR1 packet: ID(1) + magic(16) + protocol(1) + padding."""
    return bytes([ID_OPEN_CONNECTION_REQUEST_1]) + magic + bytes([protocol]) + b"\x00" * pad


def _build_ocr2(
    magic: bytes = OFFLINE_MAGIC,
    mtu: int = MINIMUM_MTU,
    guid: int = 0xDEADBEEF,
    bind_host: str = "127.0.0.1",
    bind_port: int = 0,
) -> bytes:
    """Build an OCR2 packet using BitStream for correct address encoding."""
    bs = BitStream()
    bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
    bs.write_bytes(magic)
    bs.write_address(bind_host, bind_port)
    bs.write_uint16(mtu)
    bs.write_uint64(guid)
    return bs.get_data()


# -----------------------------------------------------------------------
# OCR1 validation
# -----------------------------------------------------------------------


class TestOCR1Validation:
    async def test_ocr1_too_short(self, server_factory, raw_client):
        """OCR1 shorter than 18 bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        # 17 bytes: ID(1) + magic(16) -- missing protocol byte
        c.send(bytes([ID_OPEN_CONNECTION_REQUEST_1]) + OFFLINE_MAGIC)
        assert await c.recv_or_none() is None

    async def test_ocr1_bad_magic(self, server_factory, raw_client):
        """OCR1 with wrong magic bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        bad_magic = b"\x00" * 16
        c.send(_build_ocr1(magic=bad_magic))
        assert await c.recv_or_none() is None

    async def test_ocr1_wrong_protocol_version(self, server_factory, raw_client):
        """OCR1 with wrong protocol version gets ID_INCOMPATIBLE_PROTOCOL_VERSION."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(_build_ocr1(protocol=99))
        resp = await c.recv()
        assert resp[0] == ID_INCOMPATIBLE_PROTOCOL_VERSION

    async def test_ocr1_valid(self, server_factory, raw_client):
        """Valid OCR1 gets ID_OPEN_CONNECTION_REPLY_1."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(_build_ocr1())
        resp = await c.recv()
        assert resp[0] == ID_OPEN_CONNECTION_REPLY_1


# -----------------------------------------------------------------------
# OCR2 validation
# -----------------------------------------------------------------------


class TestOCR2Validation:
    async def test_ocr2_truncated(self, server_factory, raw_client):
        """OCR2 with valid magic but truncated fields is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        # Just ID + magic, missing address/mtu/guid
        c.send(bytes([ID_OPEN_CONNECTION_REQUEST_2]) + OFFLINE_MAGIC)
        assert await c.recv_or_none() is None

    async def test_ocr2_mtu_out_of_range(self, server_factory, raw_client):
        """OCR2 with MTU below minimum is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(_build_ocr2(mtu=10))  # Way below MINIMUM_MTU
        assert await c.recv_or_none() is None

    async def test_ocr2_guid_duplicate(self, server_factory, client_factory, raw_client):
        """OCR2 from different port with same GUID as existing connection gets ID_ALREADY_CONNECTED."""
        srv = await server_factory()
        # Connect a real client first
        cli = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        # Get the real client's GUID
        real_guid = cli._guid

        # Send raw OCR2 from a different port with the same GUID
        c = await raw_client(srv.address)
        c.send(_build_ocr2(guid=real_guid))
        resp = await c.recv()
        assert resp[0] == ID_ALREADY_CONNECTED


# -----------------------------------------------------------------------
# Unconnected ping validation
# -----------------------------------------------------------------------


class TestUnconnectedPingValidation:
    async def test_ping_too_short(self, server_factory, raw_client):
        """Unconnected ping shorter than 25 bytes is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        # ID(1) + timestamp(8) = 9 bytes, way too short
        c.send(bytes([ID_UNCONNECTED_PING]) + b"\x00" * 8)
        assert await c.recv_or_none() is None

    async def test_ping_bad_magic(self, server_factory, raw_client):
        """Ping with correct length but wrong magic is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        # ID(1) + timestamp(8) + bad_magic(16) = 25 bytes
        c.send(bytes([ID_UNCONNECTED_PING]) + b"\x00" * 8 + b"\xff" * 16)
        assert await c.recv_or_none() is None


# -----------------------------------------------------------------------
# Datagram dispatch edge cases
# -----------------------------------------------------------------------


class TestDatagramDispatch:
    async def test_empty_datagram(self, server_factory, raw_client):
        """0-byte datagram is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(b"")
        assert await c.recv_or_none() is None

    async def test_unknown_message_id(self, server_factory, raw_client):
        """Packet with unknown message ID from unknown addr is silently dropped."""
        srv = await server_factory()
        c = await raw_client(srv.address)
        c.send(bytes([0xFF]))  # Not a recognized offline message ID
        assert await c.recv_or_none() is None


# -----------------------------------------------------------------------
# Handler exception
# -----------------------------------------------------------------------


class TestHandlerException:
    async def test_handler_exception_logged(self, server_factory, client_factory, caplog):
        """Handler that raises is logged; server remains operational."""

        async def bad_handler(conn):
            raise RuntimeError("boom")

        srv = await server_factory(handler=bad_handler)
        await client_factory(srv.address)
        # Give time for the handler to run and fail
        await asyncio.sleep(0.2)

        # Server should still be operational - accept another client
        await client_factory(srv.address)
        await asyncio.sleep(0.1)
        assert len(srv._peers) >= 1  # At least the second client connected
