"""Shared fixtures and helpers for aiorak tests."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

import aiorak
from aiorak import Client, Connection, Server
from aiorak._bitstream import BitStream
from aiorak._constants import (
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
)

# ---------------------------------------------------------------------------
# Server / Client factories
# ---------------------------------------------------------------------------


@pytest.fixture
async def server_factory():
    """Factory fixture that creates servers and cleans them up after the test.

    If no handler is provided, a default echo handler is used.
    """
    servers: list[Server] = []

    async def _default_handler(conn: Connection):
        async for data in conn:
            conn.send(data)

    async def _make(
        handler: Callable[[Connection], Awaitable[None]] | None = None,
        address: tuple[str, int] = ("127.0.0.1", 0),
        max_connections: int = 64,
        **kwargs,
    ) -> Server:
        h = handler if handler is not None else _default_handler
        srv = await aiorak.create_server(address, h, max_connections=max_connections, **kwargs)
        servers.append(srv)
        return srv

    yield _make

    for srv in servers:
        await srv.close()


@pytest.fixture
async def client_factory():
    """Factory fixture that creates clients and cleans them up after the test."""
    clients: list[Client] = []

    async def _make(
        address: tuple[str, int],
        timeout: float = 5.0,
    ) -> Client:
        cli = await aiorak.connect(address, timeout=timeout)
        clients.append(cli)
        return cli

    yield _make

    for cli in clients:
        await cli.close()


@pytest.fixture
async def server_and_client(server_factory, client_factory):
    """Create a connected server+client pair with echo handler.

    Yields (server, client, server_addr).
    """
    server = await server_factory()
    addr = server.address
    client = await client_factory(addr)
    # Give the connection a moment to complete on the server side
    await asyncio.sleep(0.05)
    yield server, client, addr


# ---------------------------------------------------------------------------
# Shared async helpers
# ---------------------------------------------------------------------------


async def collect_packets(
    source: Client,
    count: int,
    timeout: float = 5.0,
) -> list[bytes]:
    """Collect *count* data packets from a Client within *timeout*."""
    packets: list[bytes] = []

    async def _gather():
        async for data in source:
            packets.append(data)
            if len(packets) >= count:
                return

    await asyncio.wait_for(_gather(), timeout=timeout)
    return packets


async def wait_for_peers(
    server: Server,
    count: int,
    timeout: float = 5.0,
) -> None:
    """Wait until *server* has at least *count* connected peers."""

    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


async def wait_for_no_peers(server: Server, timeout: float = 5.0) -> None:
    """Wait until *server* has zero connected peers."""

    async def _wait():
        while len(server._peers) > 0:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


async def noop_handler(conn: Connection) -> None:
    """Handler that consumes all incoming data without responding."""
    async for _ in conn:
        pass


def force_close_transport(target) -> None:
    """Close the underlying UDP transport without graceful disconnect."""
    target._socket._transport.close()


# ---------------------------------------------------------------------------
# Raw UDP client for crafting packets
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


# ---------------------------------------------------------------------------
# Packet-building helpers
# ---------------------------------------------------------------------------


def build_ocr1(magic: bytes = OFFLINE_MAGIC, protocol: int = RAKNET_PROTOCOL_VERSION, pad: int = 0) -> bytes:
    """Build an OCR1 packet: ID(1) + magic(16) + protocol(1) + padding."""
    return bytes([ID_OPEN_CONNECTION_REQUEST_1]) + magic + bytes([protocol]) + b"\x00" * pad


def build_ocr2(
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
