"""Shared fixtures for aiorak tests."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

import aiorak
from aiorak import Client, Connection, Server


@pytest.fixture
async def server_factory():
    """Factory fixture that creates servers and cleans them up after the test.

    If no handler is provided, a default echo handler is used.
    """
    servers: list[Server] = []

    async def _default_handler(conn: Connection):
        async for data in conn:
            await conn.send(data)

    async def _make(
        handler: Callable[[Connection], Awaitable[None]] | None = None,
        address: tuple[str, int] = ("127.0.0.1", 0),
        max_connections: int = 64,
    ) -> Server:
        h = handler if handler is not None else _default_handler
        srv = await aiorak.create_server(address, h, max_connections=max_connections)
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
    addr = server.local_address
    client = await client_factory(addr)
    # Give the connection a moment to complete on the server side
    await asyncio.sleep(0.05)
    yield server, client, addr


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


def force_close_transport(target: Server | Client) -> None:
    """Close the underlying UDP transport without graceful disconnect."""
    target._socket._transport.close()
