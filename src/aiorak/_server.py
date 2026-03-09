"""RakNet server: accept connections, dispatch per-peer handler coroutines.

The :class:`Server` class binds a UDP socket, handles the offline connection
handshake for incoming peers, and spawns a handler coroutine for each
connected peer.

Example::

    async def handler(conn: aiorak.Connection):
        async for data in conn:
            await conn.send(data)  # echo

    server = await aiorak.create_server(('0.0.0.0', 19132), handler)
    await server.serve_forever()
"""

import asyncio
import logging
import random
import time as _time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

from ._bitstream import BitStream
from ._connection import Connection, ConnectionState, _Signal
from ._constants import (
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_UNCONNECTED_PING,
    ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
    ID_UNCONNECTED_PONG,
    MAXIMUM_MTU,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
)
from ._transport import RakNetTransport, UDPSocket


class Server:
    """RakNet-compatible UDP server with per-peer handler coroutines.

    Args:
        local_address: ``(host, port)`` to bind the UDP socket to.
        handler: Async callable ``(Connection) -> None`` invoked for each new peer.
        max_connections: Maximum number of simultaneous peer connections.
        guid: 64-bit server GUID.  Generated randomly if not supplied.
        protocol_version: RakNet protocol version for handshake validation.
        max_mtu: Largest MTU accepted during handshake.
        min_mtu: Smallest MTU accepted during handshake.
    """

    def __init__(
        self,
        local_address: tuple[str, int],
        handler: Callable[[Connection], Awaitable[None]],
        max_connections: int = 64,
        guid: int | None = None,
        protocol_version: int = RAKNET_PROTOCOL_VERSION,
        max_mtu: int = MAXIMUM_MTU,
        min_mtu: int = MINIMUM_MTU,
    ) -> None:
        self._local_address = local_address
        self._handler = handler
        self._max_connections = max_connections
        self._guid = guid if guid is not None else random.getrandbits(64)
        self._protocol_version = protocol_version
        self._max_mtu = max_mtu
        self._min_mtu = min_mtu

        self._connections: dict[tuple[str, int], Connection] = {}
        self._peers: dict[tuple[str, int], Connection] = {}
        self._handler_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._socket: UDPSocket | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._closed = False
        self._closed_event: asyncio.Event = asyncio.Event()
        self._offline_ping_response: bytes = b""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind the UDP socket and start the background update loop."""
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: RakNetTransport(self._on_datagram),
            local_addr=self._local_address,
        )
        self._socket = UDPSocket(transport)
        self._update_task = asyncio.create_task(self._update_loop())

    async def serve_forever(self) -> None:
        """Block until :meth:`close` is called."""
        await self._closed_event.wait()

    async def close(self) -> None:
        """Gracefully shut down the server."""
        if self._closed:
            return
        self._closed = True

        # Disconnect all peers (queues notifications)
        for addr, conn in list(self._peers.items()):
            conn.disconnect()

        # Let the update loop flush disconnect notifications.
        if self._update_task is not None:
            try:
                await asyncio.wait_for(self._drain_all(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if self._update_task is not None:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        # Signal handlers and clean up
        for addr, conn in list(self._peers.items()):
            conn._feed_disconnect()
        for task in list(self._handler_tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._handler_tasks.clear()
        self._peers.clear()
        self._connections.clear()
        if self._socket is not None:
            self._socket.close()
        self._closed_event.set()

    async def _drain_all(self) -> None:
        """Wait until all connections have flushed outgoing data."""
        while any(c.has_pending_data for c in self._connections.values()):
            await asyncio.sleep(0.01)

    async def __aenter__(self) -> "Server":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def local_address(self) -> tuple[str, int]:
        """The actual ``(host, port)`` the server socket is bound to."""
        if self._socket is not None:
            return self._socket.local_address
        return self._local_address

    # ------------------------------------------------------------------
    # Offline ping
    # ------------------------------------------------------------------

    def set_offline_ping_response(self, data: bytes) -> None:
        """Set custom data to include in unconnected pong replies."""
        self._offline_ping_response = data

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback invoked by :class:`RakNetTransport` for each UDP datagram."""
        now = _time.monotonic()

        # Check for offline messages from unknown peers
        if addr not in self._connections:
            if self._closed:
                return  # Don't accept new connections during shutdown
            if len(data) >= 1 and data[0] in (
                ID_UNCONNECTED_PING,
                ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
            ):
                self._handle_unconnected_ping(data, addr)
                return
            if len(data) >= 1 and data[0] == ID_OPEN_CONNECTION_REQUEST_1:
                if len(self._connections) >= self._max_connections:
                    logger.warning("Max connections reached, rejecting %s", addr)
                    return
                logger.debug("New connection attempt from %s", addr)
                conn = Connection(
                    address=addr,
                    guid=self._guid,
                    is_server=True,
                    mtu=self._max_mtu,
                    protocol_version=self._protocol_version,
                    max_mtu=self._max_mtu,
                    min_mtu=self._min_mtu,
                )
                conn._system_index = len(self._connections)
                self._connections[addr] = conn

        conn = self._connections.get(addr)
        if conn is None:
            return

        responses = conn.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, addr)

        self._process_signals(addr, conn)

    def _process_signals(self, addr: tuple[str, int], conn: Connection) -> None:
        """Process connection signals and route to peers."""
        for signal, data in conn.poll_events():
            if signal == _Signal.CONNECT:
                self._peers[addr] = conn
                task = asyncio.create_task(self._run_handler(addr, conn))
                self._handler_tasks[addr] = task
            elif signal == _Signal.DISCONNECT:
                conn = self._peers.pop(addr, None)
                if conn is not None:
                    conn._feed_disconnect()
                self._connections.pop(addr, None)
                self._handler_tasks.pop(addr, None)
            elif signal == _Signal.RECEIVE:
                conn = self._peers.get(addr)
                if conn is not None:
                    conn._feed_data(data)

    async def _run_handler(self, addr: tuple[str, int], conn: Connection) -> None:
        """Run the user's handler coroutine for a peer."""
        try:
            await self._handler(conn)
        except Exception:
            logger.exception("Handler for %s raised an exception", addr)
        finally:
            self._handler_tasks.pop(addr, None)

    def _handle_unconnected_ping(self, data: bytes, addr: tuple[str, int]) -> None:
        """Respond to an unconnected ping with an unconnected pong."""
        if len(data) < 25:
            return

        bs = BitStream(data)
        msg_id = bs.read_uint8()
        ping_time = bs.read_int64()
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            return

        if msg_id == ID_UNCONNECTED_PING_OPEN_CONNECTIONS and len(self._connections) >= self._max_connections:
            return

        pong = BitStream()
        pong.write_uint8(ID_UNCONNECTED_PONG)
        pong.write_int64(ping_time)
        pong.write_uint64(self._guid)
        pong.write_bytes(OFFLINE_MAGIC)
        if self._offline_ping_response:
            pong.write_bytes(self._offline_ping_response)
        self._send_raw(pong.get_data(), addr)

    def _send_raw(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send raw bytes over the UDP socket."""
        if self._socket is not None:
            self._socket.send_to(data, addr)

    # ------------------------------------------------------------------
    # Internal: update loop
    # ------------------------------------------------------------------

    async def _update_loop(self) -> None:
        """Background task that ticks all connections every ~10 ms."""
        try:
            while True:
                if self._closed and not any(c.has_pending_data for c in self._connections.values()):
                    break
                now = _time.monotonic()

                for addr, conn in list(self._connections.items()):
                    datagrams = conn.update(now)
                    for dg in datagrams:
                        self._send_raw(dg, addr)

                    self._process_signals(addr, conn)

                    # Check for completed reliable messages
                    while True:
                        msg = conn.poll_receive()
                        if msg is None:
                            break
                        data_bytes, channel = msg
                        if data_bytes and conn.state == ConnectionState.CONNECTED:
                            conn._feed_data(data_bytes)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
