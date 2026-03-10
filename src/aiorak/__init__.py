"""aiorak — Clean-room RakNet reliable UDP implementation for Python asyncio.

This package provides an async/await API for communicating with RakNet peers
over UDP.  It implements the RakNet wire protocol from scratch, aiming for
wire compatibility with RakNet 4.x peers without porting the C++ code.

Quick start
-----------

**Server**::

    import aiorak

    async def handler(conn: aiorak.Connection):
        print(f"{conn.address} connected")
        async for data in conn:
            await conn.send(data)  # echo
        print(f"{conn.address} disconnected")

    server = await aiorak.create_server(('0.0.0.0', 19132), handler)
    await server.serve_forever()

**Client**::

    import aiorak

    client = await aiorak.connect(('127.0.0.1', 19132))
    await client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
    async for data in client:
        print("Got:", data)

Public API
----------
"""

from ._client import Client
from ._connection import Connection
from ._server import Server
from ._types import PingResponse, Priority, Reliability

__all__ = [
    "Client",
    "Connection",
    "Server",
    "PingResponse",
    "Priority",
    "Reliability",
    "create_server",
    "connect",
    "ping",
]


async def create_server(
    address: tuple[str, int],
    handler,
    max_connections: int = 64,
    *,
    guid: int | None = None,
    protocol_version: int | None = None,
    max_mtu: int | None = None,
    min_mtu: int | None = None,
    num_internal_ids: int | None = None,
) -> Server:
    """Create and start a RakNet server bound to *address*.

    Args:
        address: ``(host, port)`` to bind the UDP listener to.
        handler: Async callable invoked once per connected peer.
        max_connections: Maximum number of simultaneous peer connections.
        guid: Optional 64-bit server GUID.
        protocol_version: RakNet protocol version for handshake validation.
        max_mtu: Largest MTU accepted during handshake.
        min_mtu: Smallest MTU accepted during handshake.
        num_internal_ids: Number of internal addresses in the reliable
            handshake (default 10, some forks use 20).

    Returns:
        A started :class:`Server` instance.

    Example::

        async def handler(conn):
            async for data in conn:
                await conn.send(data)

        server = await aiorak.create_server(('0.0.0.0', 19132), handler)
    """
    kwargs: dict = {"max_connections": max_connections, "guid": guid}
    if protocol_version is not None:
        kwargs["protocol_version"] = protocol_version
    if max_mtu is not None:
        kwargs["max_mtu"] = max_mtu
    if min_mtu is not None:
        kwargs["min_mtu"] = min_mtu
    if num_internal_ids is not None:
        kwargs["num_internal_ids"] = num_internal_ids
    server = Server(address, handler, **kwargs)
    await server.start()
    return server


async def connect(
    address: tuple[str, int],
    *,
    timeout: float = 10.0,
    guid: int | None = None,
    protocol_version: int | None = None,
    max_mtu: int | None = None,
    min_mtu: int | None = None,
    mtu_discovery_sizes: tuple[int, ...] | None = None,
    num_internal_ids: int | None = None,
) -> Client:
    """Connect to a RakNet server at *address*.

    Performs MTU discovery and the full 6-message handshake sequence,
    blocking until the connection is established or *timeout* elapses.

    Args:
        address: ``(host, port)`` of the remote server.
        timeout: Maximum seconds to wait for the handshake to complete.
        guid: Optional 64-bit client GUID.
        protocol_version: RakNet protocol version for handshake validation.
        max_mtu: Largest MTU accepted during handshake.
        min_mtu: Smallest MTU accepted during handshake.
        mtu_discovery_sizes: MTU sizes attempted in order during handshake.
            Defaults to ``(max_mtu, 1200, 576)``.
        num_internal_ids: Number of internal addresses in the reliable
            handshake (default 10, some forks use 20).

    Returns:
        A connected :class:`Client` instance.

    Raises:
        asyncio.TimeoutError: If the handshake does not complete within
            *timeout* seconds.

    Example::

        client = await aiorak.connect(('127.0.0.1', 19132))
        await client.send(b"hello")
    """
    kwargs: dict = {"guid": guid}
    if protocol_version is not None:
        kwargs["protocol_version"] = protocol_version
    if max_mtu is not None:
        kwargs["max_mtu"] = max_mtu
    if min_mtu is not None:
        kwargs["min_mtu"] = min_mtu
    if mtu_discovery_sizes is not None:
        kwargs["mtu_discovery_sizes"] = mtu_discovery_sizes
    if num_internal_ids is not None:
        kwargs["num_internal_ids"] = num_internal_ids
    client = Client(address, **kwargs)
    await client.connect(timeout=timeout)
    return client


async def ping(
    address: tuple[str, int],
    *,
    timeout: float = 3.0,
    only_if_open: bool = False,
) -> PingResponse:
    """Send an unconnected ping and wait for the pong response.

    This does **not** establish a connection — it is useful for LAN server
    discovery and pre-connection latency checks.

    Args:
        address: ``(host, port)`` of the target server.
        timeout: Maximum seconds to wait for a reply.
        only_if_open: If ``True``, send ``ID_UNCONNECTED_PING_OPEN_CONNECTIONS``
            so the server only replies when it has free connection slots.

    Returns:
        A :class:`PingResponse` with the round-trip latency, server GUID,
        and any custom offline data.

    Raises:
        asyncio.TimeoutError: If no pong is received within *timeout*.

    Example::

        resp = await aiorak.ping(('127.0.0.1', 19132))
        print(f"Latency: {resp.latency_ms:.1f} ms, GUID: {resp.server_guid}")
    """
    import asyncio
    import random
    import time as _time

    from ._bitstream import BitStream
    from ._constants import (
        ID_UNCONNECTED_PING,
        ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
        ID_UNCONNECTED_PONG,
        OFFLINE_MAGIC,
    )
    from ._transport import RakNetTransport, UDPSocket

    result_future: asyncio.Future[PingResponse] = asyncio.get_running_loop().create_future()
    send_time = _time.monotonic()
    guid = random.getrandbits(64)

    def on_datagram(data: bytes, addr: tuple[str, int]) -> None:
        if result_future.done():
            return
        if len(data) < 33 or data[0] != ID_UNCONNECTED_PONG:
            return
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        _echoed_time = bs.read_int64()
        server_guid = bs.read_uint64()
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            return
        # Remaining bytes are custom offline data
        remaining = len(data) - 33
        offline_data = bs.read_bytes(remaining) if remaining > 0 else b""
        latency_ms = (_time.monotonic() - send_time) * 1000.0
        result_future.set_result(
            PingResponse(
                latency_ms=latency_ms,
                server_guid=server_guid,
                data=offline_data,
                address=addr,
            )
        )

    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: RakNetTransport(on_datagram),
        remote_addr=address,
    )
    socket = UDPSocket(transport)

    try:
        # Build unconnected ping packet
        msg_id = ID_UNCONNECTED_PING_OPEN_CONNECTIONS if only_if_open else ID_UNCONNECTED_PING
        bs = BitStream()
        bs.write_uint8(msg_id)
        bs.write_int64(int(_time.time() * 1000))
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(guid)
        socket.send_to(bs.get_data(), address)

        return await asyncio.wait_for(result_future, timeout=timeout)
    finally:
        socket.close()
