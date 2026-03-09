"""aiorak — Clean-room RakNet reliable UDP implementation for Python asyncio.

This package provides an async/await API for communicating with RakNet peers
over UDP.  It implements the RakNet wire protocol from scratch, aiming for
wire compatibility with RakNet 4.x peers without porting the C++ code.

Quick start
-----------

**Server**::

    import aiorak

    server = await aiorak.create_server(('0.0.0.0', 19132), max_connections=64)
    async for event in server:
        if event.type == aiorak.EventType.CONNECT:
            print(f"Peer connected: {event.address}")
        elif event.type == aiorak.EventType.RECEIVE:
            await server.send(event.address, b"reply",
                              reliability=aiorak.Reliability.RELIABLE_ORDERED)

**Client**::

    import aiorak

    client = await aiorak.connect(('127.0.0.1', 19132))
    await client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
    async for event in client:
        if event.type == aiorak.EventType.RECEIVE:
            print("Got:", event.data)

Public API
----------
"""

from __future__ import annotations

from typing import Optional

from ._client import Client
from ._server import Server
from ._types import Event, EventType, Priority, Reliability

__all__ = [
    "Client",
    "Server",
    "Event",
    "EventType",
    "Priority",
    "Reliability",
    "create_server",
    "connect",
]


async def create_server(
    address: tuple[str, int],
    max_connections: int = 64,
    *,
    guid: Optional[int] = None,
) -> Server:
    """Create and start a RakNet server bound to *address*.

    This is the primary entry point for server-side code.  The returned
    :class:`Server` is immediately ready to accept connections.

    Args:
        address: ``(host, port)`` to bind the UDP listener to.  Use port 0
            to let the OS pick an available port.
        max_connections: Maximum number of simultaneous peer connections.
        guid: Optional 64-bit server GUID.  A random value is generated if
            not supplied.

    Returns:
        A started :class:`Server` instance.

    Example::

        server = await aiorak.create_server(('0.0.0.0', 19132))
        print(f"Listening on {server.local_address}")
    """
    server = Server(address, max_connections=max_connections, guid=guid)
    await server.start()
    return server


async def connect(
    address: tuple[str, int],
    *,
    timeout: float = 10.0,
    guid: Optional[int] = None,
) -> Client:
    """Connect to a RakNet server at *address*.

    Performs MTU discovery and the full 6-message handshake sequence,
    blocking until the connection is established or *timeout* elapses.

    Args:
        address: ``(host, port)`` of the remote server.
        timeout: Maximum seconds to wait for the handshake to complete.
        guid: Optional 64-bit client GUID.  A random value is generated if
            not supplied.

    Returns:
        A connected :class:`Client` instance.

    Raises:
        asyncio.TimeoutError: If the handshake does not complete within
            *timeout* seconds.

    Example::

        client = await aiorak.connect(('127.0.0.1', 19132))
        await client.send(b"hello")
    """
    client = Client(address, guid=guid)
    await client.connect(timeout=timeout)
    return client
