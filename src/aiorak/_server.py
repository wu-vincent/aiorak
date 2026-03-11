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

from ._bitstream import BitStream
from ._connection import Connection, ConnectionState, _Signal
from ._constants import (
    ID_ALREADY_CONNECTED,
    ID_NO_FREE_INCOMING_CONNECTIONS,
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    ID_UNCONNECTED_PING,
    ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
    ID_UNCONNECTED_PONG,
    MAXIMUM_MTU,
    MINIMUM_MTU,
    NUMBER_OF_INTERNAL_IDS,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
)
from ._transport import RakNetTransport, UDPSocket

logger = logging.getLogger(__name__)


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
        num_internal_ids: int = NUMBER_OF_INTERNAL_IDS,
    ) -> None:
        self._local_address = local_address
        self._handler = handler
        self._max_connections = max_connections
        self._guid = guid if guid is not None else random.getrandbits(64)
        self._protocol_version = protocol_version
        self._max_mtu = max_mtu
        self._min_mtu = min_mtu
        self._num_internal_ids = num_internal_ids

        self._connections: dict[tuple[str, int], Connection] = {}
        self._guid_to_addr: dict[int, tuple[str, int]] = {}
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
        self._bound_port = self._socket.local_address[1]
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
        self._guid_to_addr.clear()
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
                    reject = BitStream()
                    reject.write_uint8(ID_NO_FREE_INCOMING_CONNECTIONS)
                    reject.write_bytes(OFFLINE_MAGIC)
                    reject.write_uint64(self._guid)
                    self._send_raw(reject.get_data(), addr)
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
                    num_internal_ids=self._num_internal_ids,
                )
                conn._system_index = len(self._connections)
                conn._local_port = self._bound_port
                self._connections[addr] = conn

        conn = self._connections.get(addr)
        if conn is None:
            return

        # GUID duplicate check at OCR2 — matches C++ RakPeer.cpp:5255-5297.
        # C++ checks both IP and GUID with a 4-way matrix before allowing
        # the connection to proceed past the offline handshake.
        if (
            len(data) >= 17
            and data[0] == ID_OPEN_CONNECTION_REQUEST_2
            and data[1:17] == OFFLINE_MAGIC
        ):
            rejection = self._check_guid_duplicate(data, addr)
            if rejection is not None:
                self._send_raw(rejection, addr)
                return

        responses = conn.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, addr)

        self._process_signals(addr, conn)

    def _check_guid_duplicate(self, data: bytes, addr: tuple[str, int]) -> bytes | None:
        """Check for GUID conflicts at OCR2, matching C++ RakPeer.cpp:5255-5297.

        C++ uses a 4-way decision matrix based on whether the IP address and
        GUID are already in use by active connections:

        - IP in use + GUID in use → resend reply if same UNVERIFIED connection,
          else ``ID_ALREADY_CONNECTED``
        - IP not in use + GUID in use → ``ID_ALREADY_CONNECTED``
        - IP in use + GUID not in use → ``ID_ALREADY_CONNECTED``
        - Neither in use → allow connection

        In Python, "IP in use" for the *same* address is handled by the normal
        Connection resend path (outcome 1 in C++).  This method catches the
        remaining cases: when a *different* address tries to reuse an existing
        GUID, or a known address arrives with a different GUID while the old
        GUID is still active.

        Returns:
            An ``ID_ALREADY_CONNECTED`` rejection packet, or ``None`` to allow.
        """
        try:
            bs = BitStream(data)
            bs.read_uint8()  # msg ID
            bs.read_bytes(16)  # OFFLINE_MAGIC
            _binding_addr = bs.read_address()
            _mtu = bs.read_uint16()
            guid = bs.read_uint64()
        except (ValueError, IndexError):
            return None  # Malformed — let Connection handle/reject

        existing_addr = self._guid_to_addr.get(guid)
        if existing_addr is not None and existing_addr != addr:
            # GUID already in use by a different address (C++ outcome 3)
            existing_conn = self._connections.get(existing_addr)
            if existing_conn is not None and existing_conn.state != ConnectionState.DISCONNECTED:
                logger.warning(
                    "GUID %016x from %s already connected from %s, rejecting",
                    guid, addr, existing_addr,
                )
                reject = BitStream()
                reject.write_uint8(ID_ALREADY_CONNECTED)
                reject.write_bytes(OFFLINE_MAGIC)
                reject.write_uint64(self._guid)
                return reject.get_data()

        # Track the GUID → address mapping
        self._guid_to_addr[guid] = addr
        return None

    def _process_signals(self, addr: tuple[str, int], conn: Connection) -> None:
        """Process connection signals and route to peers."""
        for signal, data in conn.poll_events():
            if signal == _Signal.CONNECT:
                self._peers[addr] = conn
                task = asyncio.create_task(self._run_handler(addr, conn))
                self._handler_tasks[addr] = task
            elif signal == _Signal.DISCONNECT:
                disconn_conn = self._peers.pop(addr, None)
                if disconn_conn is not None:
                    disconn_conn._feed_disconnect()
                # Clean up GUID mapping (C++ frees the RemoteSystemStruct).
                # Use the Connection from _connections if not yet promoted to peer.
                guid_conn = disconn_conn or self._connections.get(addr)
                if guid_conn is not None and guid_conn.remote_guid and self._guid_to_addr.get(guid_conn.remote_guid) == addr:
                    del self._guid_to_addr[guid_conn.remote_guid]
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
