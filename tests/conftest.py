"""Shared fixtures for aiorak tests."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

import aiorak
from aiorak import Client, Event, EventType, Server


@pytest.fixture
async def server_factory():
    """Factory fixture that creates servers and cleans them up after the test."""
    servers: list[Server] = []

    async def _make(
        address: tuple[str, int] = ("127.0.0.1", 0),
        max_connections: int = 64,
    ) -> Server:
        srv = await aiorak.create_server(address, max_connections=max_connections)
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
    """Create a connected server+client pair, yield (server, client, server_addr)."""
    server = await server_factory()
    addr = server.local_address
    client = await client_factory(addr)
    yield server, client, addr


async def collect_events(
    source: Server | Client,
    event_type: EventType,
    count: int,
    timeout: float = 5.0,
) -> list[Event]:
    """Collect *count* events of *event_type* from *source* within *timeout*."""
    events: list[Event] = []

    async def _gather():
        async for event in source:
            if event.type == event_type:
                events.append(event)
                if len(events) >= count:
                    return

    await asyncio.wait_for(_gather(), timeout=timeout)
    return events


async def collect_all_events(
    source: Server | Client,
    count: int,
    timeout: float = 5.0,
) -> list[Event]:
    """Collect *count* events of any type from *source* within *timeout*."""
    events: list[Event] = []

    async def _gather():
        async for event in source:
            events.append(event)
            if len(events) >= count:
                return

    await asyncio.wait_for(_gather(), timeout=timeout)
    return events


def force_close_transport(peer: Server | Client) -> None:
    """Close the underlying UDP transport without graceful disconnect."""
    peer._socket._transport.close()
