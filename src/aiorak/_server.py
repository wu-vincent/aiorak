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
    DEFAULT_TIMEOUT,
    ID_ALREADY_CONNECTED,
    ID_INCOMPATIBLE_PROTOCOL_VERSION,
    ID_IP_RECENTLY_CONNECTED,
    ID_NO_FREE_INCOMING_CONNECTIONS,
    ID_OPEN_CONNECTION_REPLY_1,
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
    UDP_HEADER_SIZE,
)
from ._transport import RakNetTransport, UDPSocket
from ._types import Reliability

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
        timeout: Default timeout in seconds for new connections.  Matches
            C++ ``defaultTimeoutTime`` (``RakPeer.cpp:247``).
        rate_limit_ips: If ``True``, reject OCR2 from IPs
            that connected within the last 100 ms.  Matches C++
            ``SetLimitIPConnectionFrequency()`` (``RakPeer.cpp:3604-3623``).
    """

    # Minimum interval between connections from the same IP (milliseconds).
    # Matches the hard-coded 100 ms in C++ RakPeer.cpp:3613.
    _CONNECTION_FREQUENCY_MS: int = 100

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
        timeout: float = DEFAULT_TIMEOUT,
        rate_limit_ips: bool = False,
    ) -> None:
        self._local_address = local_address
        self._handler = handler
        self._max_connections = max_connections
        self._guid = guid if guid is not None else random.getrandbits(64)
        self._protocol_version = protocol_version
        self._max_mtu = max_mtu
        self._min_mtu = min_mtu
        self._num_internal_ids = num_internal_ids
        self._timeout = timeout
        self._rate_limit_ips = rate_limit_ips

        self._connections: dict[tuple[str, int], Connection] = {}
        self._guid_to_addr: dict[int, tuple[str, int]] = {}
        # Tracks the last time each IP (host only, ignoring port) was assigned
        # a Connection slot, for connection frequency limiting.
        # Matches C++ remoteSystemList[i].connectionTime (RakPeer.cpp:3612).
        self._ip_connection_times: dict[str, float] = {}
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

    async def close(self, *, notify: bool = True) -> None:
        """Shut down the server.

        Args:
            notify: If ``True`` (default), send ``ID_DISCONNECTION_NOTIFICATION``
                to each connected peer before shutting down.  If ``False``,
                silently drop all connections — clients will detect the loss
                via timeout.
        """
        if self._closed:
            return
        self._closed = True

        if notify:
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
        self._ip_connection_times.clear()
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

    @property
    def timeout(self) -> float:
        """Default dead-connection timeout in seconds.

        Delegates to :meth:`get_timeout` / :meth:`set_timeout`.
        """
        return self.get_timeout()

    @timeout.setter
    def timeout(self, value: float) -> None:
        self.set_timeout(value)

    # ------------------------------------------------------------------
    # Offline ping
    # ------------------------------------------------------------------

    def set_offline_ping_response(self, data: bytes) -> None:
        """Set custom data to include in unconnected pong replies."""
        self._offline_ping_response = data

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    def broadcast(
        self,
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
        *,
        exclude: Connection | None = None,
    ) -> None:
        """Send a message to all connected peers.

        Matches C++ ``RakPeer::Send()`` with ``broadcast=true``
        (``RakPeer.cpp:4330-4347``): iterates all active remote systems and
        sends to each, optionally skipping one peer.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.  Defaults to ``RELIABLE_ORDERED``.
            channel: Ordering channel (0–31).
            exclude: If set, skip this connection (e.g. to relay a message
                to everyone except the sender).
        """
        for conn in self._peers.values():
            if conn is exclude:
                continue
            if conn.state == ConnectionState.CONNECTED:
                conn._send(data, reliability, channel)

    # ------------------------------------------------------------------
    # Per-peer disconnect
    # ------------------------------------------------------------------

    def disconnect(self, conn: Connection, *, notify: bool = True) -> None:
        """Disconnect a specific peer from the server.

        Matches C++ ``RakPeer::CloseConnection()`` (``RakPeer.cpp:1650-1679``).

        Args:
            conn: The connection to disconnect.
            notify: If ``True`` (default), send ``ID_DISCONNECTION_NOTIFICATION``
                to the peer before closing.  If ``False``, drop silently.
        """
        if conn.state == ConnectionState.CONNECTED:
            if notify:
                conn.disconnect()  # sends ID_DISCONNECTION_NOTIFICATION, transitions to DISCONNECTING
            else:
                conn.state = ConnectionState.DISCONNECTED
                conn._events.append((_Signal.DISCONNECT, b""))

    # ------------------------------------------------------------------
    # Timeout configuration
    # ------------------------------------------------------------------

    def set_timeout(self, timeout: float, conn: Connection | None = None) -> None:
        """Set the timeout for a specific connection or all connections.

        Matches C++ ``RakPeer::SetTimeoutTime()`` (``RakPeer.cpp:2524-2546``).
        When *conn* is ``None``, updates the default and all active connections.

        Args:
            timeout: Timeout in seconds.
            conn: If set, only update this connection.  Otherwise update the
                default and all active connections.
        """
        if conn is not None:
            conn.timeout = timeout
        else:
            self._timeout = timeout
            for c in self._connections.values():
                c.timeout = timeout

    def get_timeout(self, conn: Connection | None = None) -> float:
        """Return the timeout for a connection or the server default.

        Matches C++ ``RakPeer::GetTimeoutTime()`` (``RakPeer.cpp:2551-2564``).
        """
        if conn is not None:
            return conn.timeout
        return self._timeout

    # ------------------------------------------------------------------
    # MTU query
    # ------------------------------------------------------------------

    def get_mtu(self, conn: Connection) -> int:
        """Return the negotiated MTU for a connection.

        Matches C++ ``RakPeer::GetMTUSize()`` (``RakPeer.cpp:2572``).
        """
        return conn.mtu

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback invoked by :class:`RakNetTransport` for each UDP datagram."""
        now = _time.monotonic()

        # ---- Offline messages from unknown peers ----
        if addr not in self._connections:
            if self._closed:
                return
            if len(data) < 1:
                return
            if data[0] in (ID_UNCONNECTED_PING, ID_UNCONNECTED_PING_OPEN_CONNECTIONS):
                self._handle_unconnected_ping(data, addr)
                return
            # OCR1 is handled statelessly — no Connection object is created.
            # This matches C++ RakPeer.cpp:5127-5197 which only validates the
            # protocol version, computes MTU, and sends OR1.  No
            # RemoteSystemStruct is allocated until OCR2.
            if data[0] == ID_OPEN_CONNECTION_REQUEST_1:
                self._handle_open_request_1(data, addr)
                return
            # OCR2 from a new address — create the Connection now.
            if len(data) >= 17 and data[0] == ID_OPEN_CONNECTION_REQUEST_2 and data[1:17] == OFFLINE_MAGIC:
                conn = self._create_connection_from_ocr2(data, addr, now)
                if conn is None:
                    return  # Rejected (capacity / GUID / frequency)
            else:
                return  # Unknown peer, not an offline message — drop

        conn = self._connections.get(addr)
        if conn is None:
            return

        responses = conn.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, addr)

        self._process_signals(addr, conn)

    def _handle_open_request_1(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle OCR1 statelessly, matching C++ RakPeer.cpp:5127-5197.

        No ``Connection`` is allocated.  Only validates the protocol version,
        computes the MTU from the packet size, and sends ``OR1`` back.
        """
        if len(data) < 18:  # 1 ID + 16 magic + 1 protocol version minimum
            return
        if data[1:17] != OFFLINE_MAGIC:
            return

        proto = data[17]
        if proto != self._protocol_version:
            logger.warning("Open request 1 from %s: protocol %d != %d", addr, proto, self._protocol_version)
            reject = BitStream()
            reject.write_uint8(ID_INCOMPATIBLE_PROTOCOL_VERSION)
            reject.write_uint8(self._protocol_version)
            reject.write_bytes(OFFLINE_MAGIC)
            reject.write_uint64(self._guid)
            self._send_raw(reject.get_data(), addr)
            return

        # MTU = total datagram size + UDP header, capped to max_mtu
        # (C++ RakPeer.cpp:5183-5186)
        incoming_mtu = min(len(data) + UDP_HEADER_SIZE, self._max_mtu)

        reply = BitStream()
        reply.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        reply.write_bytes(OFFLINE_MAGIC)
        reply.write_uint64(self._guid)
        reply.write_uint8(0)  # has_security = false
        reply.write_uint16(incoming_mtu)
        self._send_raw(reply.get_data(), addr)

    def _create_connection_from_ocr2(self, data: bytes, addr: tuple[str, int], now: float) -> Connection | None:
        """Parse OCR2, run all server-side checks, and create a Connection.

        Checks performed (in C++ order from RakPeer.cpp:5198-5384):

        1. GUID duplicate check (RakPeer.cpp:5255-5297)
        2. Capacity check — ``AllowIncomingConnections()`` (RakPeer.cpp:5349)
        3. Connection frequency limiting (RakPeer.cpp:3604-3623)

        Returns:
            The new ``Connection`` on success, or ``None`` if rejected.
        """
        # --- Parse OCR2 fields ---
        try:
            bs = BitStream(data)
            bs.read_uint8()  # msg ID
            bs.read_bytes(16)  # OFFLINE_MAGIC (already verified by caller)
            _binding_addr = bs.read_address()
            mtu = bs.read_uint16()
            guid = bs.read_uint64()
        except (ValueError, IndexError):
            return None

        if not (self._min_mtu <= mtu <= self._max_mtu):
            return None

        # --- 1. GUID duplicate check (RakPeer.cpp:5255-5297) ---
        existing_addr = self._guid_to_addr.get(guid)
        if existing_addr is not None and existing_addr != addr:
            existing_conn = self._connections.get(existing_addr)
            if existing_conn is not None and existing_conn.state != ConnectionState.DISCONNECTED:
                logger.warning(
                    "GUID %016x from %s already connected from %s, rejecting",
                    guid,
                    addr,
                    existing_addr,
                )
                reject = BitStream()
                reject.write_uint8(ID_ALREADY_CONNECTED)
                reject.write_bytes(OFFLINE_MAGIC)
                reject.write_uint64(self._guid)
                self._send_raw(reject.get_data(), addr)
                return None

        # --- 2. Capacity check (RakPeer.cpp:5349) ---
        if len(self._peers) >= self._max_connections:
            logger.warning("Max connections reached, rejecting %s at OCR2", addr)
            reject = BitStream()
            reject.write_uint8(ID_NO_FREE_INCOMING_CONNECTIONS)
            reject.write_bytes(OFFLINE_MAGIC)
            reject.write_uint64(self._guid)
            self._send_raw(reject.get_data(), addr)
            return None

        # --- 3. Connection frequency limiting (RakPeer.cpp:3604-3623) ---
        # C++ checks whether any active remoteSystem with the same IP
        # (ignoring port) connected within the last 100 ms.
        if self._rate_limit_ips:
            host = addr[0]
            if host not in ("127.0.0.1", "::1"):  # C++ skips loopback
                last_time = self._ip_connection_times.get(host, 0.0)
                if (now - last_time) * 1000 < self._CONNECTION_FREQUENCY_MS:
                    logger.warning("IP %s connected too recently, rejecting", host)
                    reject = BitStream()
                    reject.write_uint8(ID_IP_RECENTLY_CONNECTED)
                    reject.write_bytes(OFFLINE_MAGIC)
                    reject.write_uint64(self._guid)
                    self._send_raw(reject.get_data(), addr)
                    return None
            self._ip_connection_times[host] = now

        # --- All checks passed — create Connection ---
        self._guid_to_addr[guid] = addr
        logger.debug("New connection from %s (GUID %016x)", addr, guid)
        conn = Connection(
            address=addr,
            guid=self._guid,
            is_server=True,
            mtu=mtu,
            timeout=self._timeout,
            protocol_version=self._protocol_version,
            max_mtu=self._max_mtu,
            min_mtu=self._min_mtu,
            num_internal_ids=self._num_internal_ids,
        )
        conn._system_index = len(self._connections)
        conn._local_port = self._bound_port
        conn.remote_guid = guid
        # Set CONNECTING state and handshake timing — previously done by
        # _handle_open_request_1, now skipped since OCR1 is stateless.
        conn.state = ConnectionState.CONNECTING
        conn._handshake_start = now
        conn._last_recv_time = now
        self._connections[addr] = conn
        return conn

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
                if (
                    guid_conn is not None
                    and guid_conn.remote_guid
                    and self._guid_to_addr.get(guid_conn.remote_guid) == addr
                ):
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

        if msg_id == ID_UNCONNECTED_PING_OPEN_CONNECTIONS and len(self._peers) >= self._max_connections:
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
