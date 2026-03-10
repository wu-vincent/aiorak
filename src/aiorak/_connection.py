"""Per-peer connection state machine and handshake logic.

Each remote peer is represented by a :class:`Connection` instance that tracks
the connection lifecycle from initial handshake through data exchange to
graceful (or timeout-based) disconnection.

State transitions::

    DISCONNECTED ─► CONNECTING ─► CONNECTED ─► DISCONNECTING ─► DISCONNECTED

The :class:`Connection` owns a :class:`~aiorak._reliability.ReliabilityLayer`
and a :class:`~aiorak._congestion.CongestionController` that handle the
reliable-UDP mechanics.
"""

import asyncio
import enum
import logging
import time as _time
from collections.abc import AsyncIterator

from ._bitstream import BitStream
from ._congestion import CongestionController
from ._constants import (
    DEFAULT_TIMEOUT,
    ID_CONNECTED_PING,
    ID_CONNECTED_PONG,
    ID_CONNECTION_REQUEST,
    ID_CONNECTION_REQUEST_ACCEPTED,
    ID_DETECT_LOST_CONNECTIONS,
    ID_DISCONNECTION_NOTIFICATION,
    ID_NEW_INCOMING_CONNECTION,
    ID_OPEN_CONNECTION_REPLY_1,
    ID_OPEN_CONNECTION_REPLY_2,
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    MAXIMUM_MTU,
    MINIMUM_MTU,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
    UDP_HEADER_SIZE,
)
from ._reliability import ReliabilityLayer
from ._types import Reliability

logger = logging.getLogger(__name__)


class _Signal(enum.IntEnum):
    """Internal lifecycle signals (not part of the public API)."""

    CONNECT = 0
    DISCONNECT = 1
    RECEIVE = 2


class ConnectionState(enum.IntEnum):
    """Lifecycle states for a connection to a remote peer."""

    DISCONNECTED = 0
    """No active connection."""

    CONNECTING = 1
    """Handshake in progress (offline or reliable phase)."""

    CONNECTED = 2
    """Fully connected — application data can be exchanged."""

    DISCONNECTING = 3
    """Graceful disconnect initiated; waiting for final ACK."""


class Connection:
    """State machine and protocol handler for a single remote peer.

    A ``Connection`` can operate in either *server mode* (responding to
    incoming handshake requests) or *client mode* (initiating the handshake).

    Args:
        address: ``(host, port)`` of the remote peer.
        guid: Our own 64-bit GUID.
        is_server: ``True`` if we are the server side of this connection.
        mtu: Initial MTU to use (may be refined during handshake).
        timeout: Seconds of silence before declaring the connection lost.
        protocol_version: RakNet protocol version for handshake validation.
        max_mtu: Largest MTU accepted during handshake.
        min_mtu: Smallest MTU accepted during handshake.
        mtu_discovery_sizes: MTU sizes attempted in order during client
            connection handshake.  Defaults to ``(max_mtu, 1200, 576)``.
    """

    def __init__(
        self,
        address: tuple[str, int],
        guid: int,
        *,
        is_server: bool = False,
        mtu: int = MAXIMUM_MTU,
        timeout: float = DEFAULT_TIMEOUT,
        protocol_version: int = RAKNET_PROTOCOL_VERSION,
        max_mtu: int = MAXIMUM_MTU,
        min_mtu: int = MINIMUM_MTU,
        mtu_discovery_sizes: tuple[int, ...] | None = None,
    ) -> None:
        self.address = address
        self.guid = guid
        self.remote_guid: int = 0
        self.is_server = is_server
        self.state = ConnectionState.DISCONNECTED
        self.mtu = mtu
        self.timeout = timeout
        self._protocol_version = protocol_version
        self._max_mtu = max_mtu
        self._min_mtu = min_mtu
        self._mtu_discovery_sizes = mtu_discovery_sizes if mtu_discovery_sizes is not None else (max_mtu, 1200, 576)

        self._cc = CongestionController(mtu)
        self._reliability = ReliabilityLayer(mtu, self._cc, timeout=timeout)

        self._last_recv_time: float = 0.0
        self._last_ping_time: float = 0.0
        self._ping_interval: float = 5.0

        # Client handshake tracking
        self._mtu_attempt_index: int = 0
        self._handshake_start: float = 0.0
        self._handshake_retransmit_time: float = 0.0

        # Server handshake tracking
        self._system_index: int = 0

        # Pending signals for the application layer
        self._events: list[tuple[_Signal, bytes]] = []

        # Async packet queue for user-facing iteration
        self._packet_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def send(
        self, data: bytes, reliability: Reliability = Reliability.RELIABLE_ORDERED, channel: int = 0
    ) -> None:
        """Send a message to this peer.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            channel: Ordering channel (0–31).

        Raises:
            RuntimeError: If the connection is not in the ``CONNECTED`` state.
        """
        if self._closed:
            raise RuntimeError("Connection is closed")
        self._send(data, reliability, channel)

    async def recv(self) -> bytes:
        """Wait for and return the next received packet.

        Returns:
            The raw payload bytes.

        Raises:
            ConnectionError: If the peer disconnected.
        """
        data = await self._packet_queue.get()
        if data is None:
            raise ConnectionError("Peer disconnected")
        return data

    async def close(self) -> None:
        """Disconnect this peer."""
        if not self._closed:
            self._closed = True
            self.disconnect()

    def _feed_data(self, data: bytes) -> None:
        """Push received data into the packet queue (called internally)."""
        self._packet_queue.put_nowait(data)

    def _feed_disconnect(self) -> None:
        """Signal disconnect by pushing a None sentinel (called internally)."""
        self._packet_queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield received data packets until the peer disconnects."""
        while True:
            data = await self._packet_queue.get()
            if data is None:
                break
            yield data

    # ------------------------------------------------------------------
    # Internal send
    # ------------------------------------------------------------------

    def _send(self, data: bytes, reliability: Reliability = Reliability.RELIABLE_ORDERED, channel: int = 0) -> None:
        """Enqueue user data for transmission (sync, internal).

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            channel: Ordering channel (0–31).

        Raises:
            RuntimeError: If the connection is not in the ``CONNECTED`` state.
        """
        if self.state != ConnectionState.CONNECTED:
            raise RuntimeError(f"Cannot send: connection is {self.state.name}")
        self._reliability.send(data, reliability, channel)

    @property
    def has_pending_data(self) -> bool:
        """True if the reliability layer still has outgoing data."""
        return self._reliability.has_pending_data

    def poll_events(self) -> list[tuple[_Signal, bytes]]:
        """Drain and return pending application-level signals.

        Returns:
            A list of ``(_Signal, data_bytes)`` tuples.
        """
        events = list(self._events)
        self._events.clear()
        return events

    def poll_receive(self) -> tuple[bytes, int] | None:
        """Pop the next completed message from the reliability layer.

        Returns:
            ``(data, channel)`` or ``None``.
        """
        return self._reliability.poll_receive()

    # ------------------------------------------------------------------
    # Datagram ingress
    # ------------------------------------------------------------------

    def on_datagram(self, data: bytes, now: float) -> list[bytes]:
        """Handle an incoming UDP datagram from this peer.

        Dispatches to the appropriate handler based on the connection state
        and the datagram content.

        Args:
            data: Raw UDP payload.
            now: Current monotonic time in seconds.

        Returns:
            A list of response datagrams to send back (may be empty).
        """
        self._last_recv_time = now
        outgoing: list[bytes] = []

        if self.state in (ConnectionState.DISCONNECTED, ConnectionState.CONNECTING):
            # May be an offline handshake message
            if self._is_offline_message(data):
                resp = self._handle_offline(data, now)
                if resp is not None:
                    outgoing.append(resp)
                return outgoing

        if self.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED, ConnectionState.DISCONNECTING):
            self._reliability.on_datagram_received(data, now)
            # Process any completed messages
            while True:
                msg = self._reliability.poll_receive()
                if msg is None:
                    break
                resp = self._handle_connected_message(msg[0], now)
                if resp is not None:
                    outgoing.extend(resp)

        return outgoing

    # ------------------------------------------------------------------
    # Periodic update
    # ------------------------------------------------------------------

    def update(self, now: float) -> list[bytes]:
        """Run a periodic update tick.

        Checks for timeouts, sends pings, and flushes the reliability layer.

        Args:
            now: Current monotonic time in seconds.

        Returns:
            A list of datagram bytes to transmit.
        """
        outgoing: list[bytes] = []

        # Timeout check
        if self.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED, ConnectionState.DISCONNECTING):
            if self._last_recv_time > 0 and now - self._last_recv_time > self.timeout:
                logger.warning("Connection to %s timed out after %.1fs", self.address, self.timeout)
                self._events.append((_Signal.DISCONNECT, b""))
                self.state = ConnectionState.DISCONNECTED
                return outgoing

        # Client handshake retransmit
        if (
            self.state == ConnectionState.CONNECTING
            and not self.is_server
            and now - self._handshake_retransmit_time > 1.0
        ):
            pkt = self._build_open_request_1(now)
            if pkt is not None:
                outgoing.append(pkt)
                self._handshake_retransmit_time = now

        # Connected ping
        if self.state == ConnectionState.CONNECTED and now - self._last_ping_time > self._ping_interval:
            self._send_connected_ping(now)
            self._last_ping_time = now

        # Reliability layer flush
        if self.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED, ConnectionState.DISCONNECTING):
            outgoing.extend(self._reliability.update(now))

        # If disconnecting and all outgoing data has been flushed, complete the disconnect
        if self.state == ConnectionState.DISCONNECTING and not self._reliability.has_pending_data:
            self.state = ConnectionState.DISCONNECTED
            self._events.append((_Signal.DISCONNECT, b""))

        return outgoing

    # ------------------------------------------------------------------
    # Client-side handshake initiation
    # ------------------------------------------------------------------

    def start_connect(self, now: float) -> bytes:
        """Begin the client-side connection handshake.

        Builds and returns the first ``ID_OPEN_CONNECTION_REQUEST_1`` packet
        with MTU discovery padding.

        Args:
            now: Current monotonic time in seconds.

        Returns:
            The raw offline handshake packet to send.
        """
        self.state = ConnectionState.CONNECTING
        self._handshake_start = now
        self._last_recv_time = now
        self._mtu_attempt_index = 0
        self._handshake_retransmit_time = now
        return self._build_open_request_1(now)

    def _build_open_request_1(self, now: float) -> bytes | None:
        """Build an ``ID_OPEN_CONNECTION_REQUEST_1`` packet.

        Args:
            now: Current monotonic time.

        Returns:
            The raw packet bytes, or ``None`` if all MTU sizes exhausted.
        """
        if self._mtu_attempt_index >= len(self._mtu_discovery_sizes):
            return None

        mtu = self._mtu_discovery_sizes[self._mtu_attempt_index]
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(self._protocol_version)
        # Pad to MTU (minus UDP header)
        target = mtu - UDP_HEADER_SIZE
        bs.pad_with_zero_to_byte_length(target)
        return bs.get_data()

    # ------------------------------------------------------------------
    # Offline handshake message handling
    # ------------------------------------------------------------------

    _OFFLINE_MSG_IDS = frozenset(
        {
            ID_OPEN_CONNECTION_REQUEST_1,
            ID_OPEN_CONNECTION_REPLY_1,
            ID_OPEN_CONNECTION_REQUEST_2,
            ID_OPEN_CONNECTION_REPLY_2,
        }
    )

    def _is_offline_message(self, data: bytes) -> bool:
        """Return ``True`` if *data* starts with a known offline message ID.

        Args:
            data: Raw UDP payload.
        """
        return len(data) >= 1 and data[0] in self._OFFLINE_MSG_IDS

    def _handle_offline(self, data: bytes, now: float) -> bytes | None:
        """Route offline (unconnected) handshake messages.

        Args:
            data: Raw datagram bytes.
            now: Current monotonic time.

        Returns:
            A response packet, or ``None`` if the datagram is not an offline
            handshake message.
        """
        if len(data) < 1:
            return None
        msg_id = data[0]

        if msg_id == ID_OPEN_CONNECTION_REQUEST_1 and self.is_server:
            return self._handle_open_request_1(data, now)
        elif msg_id == ID_OPEN_CONNECTION_REPLY_1 and not self.is_server:
            return self._handle_open_reply_1(data, now)
        elif msg_id == ID_OPEN_CONNECTION_REQUEST_2 and self.is_server:
            return self._handle_open_request_2(data, now)
        elif msg_id == ID_OPEN_CONNECTION_REPLY_2 and not self.is_server:
            return self._handle_open_reply_2(data, now)
        return None

    def _handle_open_request_1(self, data: bytes, now: float) -> bytes | None:
        """Handle ``ID_OPEN_CONNECTION_REQUEST_1`` (server side).

        Validates the magic bytes and protocol version, then replies with
        ``ID_OPEN_CONNECTION_REPLY_1``.

        Args:
            data: Raw packet bytes.
            now: Current monotonic time.

        Returns:
            ``ID_OPEN_CONNECTION_REPLY_1`` response, or ``None`` on error.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            logger.warning("Open request 1 from %s: invalid magic", self.address)
            return None
        proto = bs.read_uint8()
        if proto != self._protocol_version:
            logger.warning("Open request 1 from %s: protocol %d != %d", self.address, proto, self._protocol_version)
            return None

        # MTU = total datagram size + UDP header
        incoming_mtu = len(data) + UDP_HEADER_SIZE

        self.state = ConnectionState.CONNECTING
        logger.debug("State -> CONNECTING for %s", self.address)
        self._last_recv_time = now

        # Build reply
        reply = BitStream()
        reply.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        reply.write_bytes(OFFLINE_MAGIC)
        reply.write_uint64(self.guid)
        reply.write_uint8(0)  # has_security = false
        reply.write_uint16(incoming_mtu)
        return reply.get_data()

    def _handle_open_reply_1(self, data: bytes, now: float) -> bytes | None:
        """Handle ``ID_OPEN_CONNECTION_REPLY_1`` (client side).

        Extracts the server GUID and negotiated MTU, then sends
        ``ID_OPEN_CONNECTION_REQUEST_2``.

        Args:
            data: Raw packet bytes.
            now: Current monotonic time.

        Returns:
            ``ID_OPEN_CONNECTION_REQUEST_2`` response.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            logger.warning("Open reply 1 from %s: invalid magic", self.address)
            return None
        self.remote_guid = bs.read_uint64()
        has_security = bs.read_uint8()
        if has_security:
            return None  # Security not supported
        mtu = bs.read_uint16()
        if not (self._min_mtu <= mtu <= self._max_mtu):
            logger.warning(
                "Open reply 1 from %s: MTU %d out of range [%d, %d]", self.address, mtu, self._min_mtu, self._max_mtu
            )
            return None
        self.mtu = mtu

        # Update reliability layer and CC with negotiated MTU
        self._cc.mtu = mtu
        self._reliability._mtu = mtu

        # Build request 2
        reply = BitStream()
        reply.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        reply.write_bytes(OFFLINE_MAGIC)
        # Server binding address
        reply.write_address(self.address[0], self.address[1])
        reply.write_uint16(mtu)
        reply.write_uint64(self.guid)
        return reply.get_data()

    def _handle_open_request_2(self, data: bytes, now: float) -> bytes | None:
        """Handle ``ID_OPEN_CONNECTION_REQUEST_2`` (server side).

        Extracts the client GUID and MTU, then replies with
        ``ID_OPEN_CONNECTION_REPLY_2`` and transitions to the reliable
        handshake phase.

        Args:
            data: Raw packet bytes.
            now: Current monotonic time.

        Returns:
            ``ID_OPEN_CONNECTION_REPLY_2`` response.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            logger.warning("Open request 2 from %s: invalid magic", self.address)
            return None
        _server_addr = bs.read_address()
        mtu = bs.read_uint16()
        if not (self._min_mtu <= mtu <= self._max_mtu):
            logger.warning(
                "Open request 2 from %s: MTU %d out of range [%d, %d]", self.address, mtu, self._min_mtu, self._max_mtu
            )
            return None
        self.remote_guid = bs.read_uint64()
        self.mtu = mtu

        self._cc.mtu = mtu
        self._reliability._mtu = mtu

        # Build reply 2
        reply = BitStream()
        reply.write_uint8(ID_OPEN_CONNECTION_REPLY_2)
        reply.write_bytes(OFFLINE_MAGIC)
        reply.write_uint64(self.guid)
        # Client address
        reply.write_address(self.address[0], self.address[1])
        reply.write_uint16(mtu)
        reply.write_bit(False)  # has_security = false (1 bit, matches C++ Write(bool))
        return reply.get_data()

    def _handle_open_reply_2(self, data: bytes, now: float) -> bytes | None:
        """Handle ``ID_OPEN_CONNECTION_REPLY_2`` (client side).

        Completes the offline handshake phase and sends ``ID_CONNECTION_REQUEST``
        over the reliability layer to begin the reliable handshake.

        Args:
            data: Raw packet bytes.
            now: Current monotonic time.

        Returns:
            ``None`` — the connection request is sent via the reliability layer.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            return None
        self.remote_guid = bs.read_uint64()
        _binding_addr = bs.read_address()
        mtu = bs.read_uint16()
        _do_security = bs.read_bit()  # 1 bit (matches C++ Read(bool))
        self.mtu = mtu

        self._cc.mtu = mtu
        self._reliability._mtu = mtu

        # Send ID_CONNECTION_REQUEST over reliable channel
        req = BitStream()
        req.write_uint8(ID_CONNECTION_REQUEST)
        req.write_uint64(self.guid)
        req.write_int64(int(_time.time() * 1000))  # timestamp in ms
        req.write_uint8(0)  # has_security = false
        self._reliability.send(req.get_data(), Reliability.RELIABLE)
        return None

    # ------------------------------------------------------------------
    # Connected (reliable) message handling
    # ------------------------------------------------------------------

    def _handle_connected_message(self, data: bytes, now: float) -> list[bytes] | None:
        """Route a message received through the reliability layer.

        Handles internal protocol messages (connection request/accepted,
        ping/pong) and delivers user messages as events.

        Args:
            data: Decoded message payload.
            now: Current monotonic time.

        Returns:
            Optional list of response datagrams (usually ``None`` — responses
            are queued via the reliability layer).
        """
        if len(data) < 1:
            return None
        msg_id = data[0]

        # Handshake messages — only valid during the connecting phase
        if msg_id == ID_CONNECTION_REQUEST and self.is_server and self.state == ConnectionState.CONNECTING:
            return self._handle_connection_request(data, now)
        elif (
            msg_id == ID_CONNECTION_REQUEST_ACCEPTED and not self.is_server and self.state == ConnectionState.CONNECTING
        ):
            return self._handle_connection_accepted(data, now)
        elif msg_id == ID_NEW_INCOMING_CONNECTION and self.is_server and self.state == ConnectionState.CONNECTING:
            self.state = ConnectionState.CONNECTED
            logger.debug("State -> CONNECTED for %s", self.address)
            self._events.append((_Signal.CONNECT, b""))
            return None

        # Internal connected messages — validate expected size to avoid
        # false positives on user data that happens to start with the same
        # byte value (e.g. ID_CONNECTED_PING = 0x00).
        elif msg_id == ID_CONNECTED_PING and len(data) == 9:
            return self._handle_connected_ping(data, now)
        elif msg_id == ID_CONNECTED_PONG and len(data) == 17:
            return None  # Pong received — RTT already updated by ACK layer
        elif msg_id == ID_DISCONNECTION_NOTIFICATION and len(data) == 1:
            self.state = ConnectionState.DISCONNECTED
            self._events.append((_Signal.DISCONNECT, b""))
            return None
        elif msg_id == ID_DETECT_LOST_CONNECTIONS and len(data) == 1:
            return None  # ACK is implicit
        else:
            # User message
            if self.state == ConnectionState.CONNECTED:
                self._events.append((_Signal.RECEIVE, data))
            return None

    def _handle_connection_request(self, data: bytes, now: float) -> list[bytes] | None:
        """Handle ``ID_CONNECTION_REQUEST`` (server side, reliable phase).

        Sends ``ID_CONNECTION_REQUEST_ACCEPTED`` back through the reliability
        layer.

        Args:
            data: Full message payload.
            now: Current monotonic time.

        Returns:
            ``None`` — the response is queued in the reliability layer.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        client_guid = bs.read_uint64()
        client_time = bs.read_int64()
        _has_security = bs.read_uint8()
        self.remote_guid = client_guid

        # Build ID_CONNECTION_REQUEST_ACCEPTED
        reply = BitStream()
        reply.write_uint8(ID_CONNECTION_REQUEST_ACCEPTED)
        # Client address
        reply.write_address(self.address[0], self.address[1])
        # System index
        reply.write_uint16(self._system_index)
        # 10 internal addresses (6 bytes each = 7 bytes with address family)
        for _ in range(10):
            reply.write_address("127.0.0.1", 0)
        # Timestamps: client's sent time + server time
        reply.write_int64(client_time)
        reply.write_int64(int(_time.time() * 1000))

        self._reliability.send(reply.get_data(), Reliability.RELIABLE)
        return None

    def _handle_connection_accepted(self, data: bytes, now: float) -> list[bytes] | None:
        """Handle ``ID_CONNECTION_REQUEST_ACCEPTED`` (client side, reliable phase).

        Transitions to ``CONNECTED`` state and sends
        ``ID_NEW_INCOMING_CONNECTION`` to the server.

        Args:
            data: Full message payload.
            now: Current monotonic time.

        Returns:
            ``None`` — the response is queued in the reliability layer.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        _our_addr = bs.read_address()
        _sys_idx = bs.read_uint16()
        # Skip 10 internal addresses
        for _ in range(10):
            bs.read_address()
        _sent_time = bs.read_int64()
        _reply_time = bs.read_int64()

        self.state = ConnectionState.CONNECTED
        logger.debug("State -> CONNECTED for %s", self.address)
        self._events.append((_Signal.CONNECT, b""))

        # Send ID_NEW_INCOMING_CONNECTION
        reply = BitStream()
        reply.write_uint8(ID_NEW_INCOMING_CONNECTION)
        reply.write_address(self.address[0], self.address[1])
        # 10 internal addresses
        for _ in range(10):
            reply.write_address("127.0.0.1", 0)
        reply.write_int64(_reply_time)
        reply.write_int64(int(_time.time() * 1000))

        self._reliability.send(reply.get_data(), Reliability.RELIABLE)
        return None

    # ------------------------------------------------------------------
    # Ping / Pong
    # ------------------------------------------------------------------

    def _send_connected_ping(self, now: float) -> None:
        """Send an ``ID_CONNECTED_PING`` through the reliability layer.

        Args:
            now: Current monotonic time.
        """
        bs = BitStream()
        bs.write_uint8(ID_CONNECTED_PING)
        bs.write_int64(int(now * 1000))
        self._reliability.send(bs.get_data(), Reliability.UNRELIABLE)

    def _handle_connected_ping(self, data: bytes, now: float) -> list[bytes] | None:
        """Handle ``ID_CONNECTED_PING`` and reply with ``ID_CONNECTED_PONG``.

        Args:
            data: Ping message payload.
            now: Current monotonic time.

        Returns:
            ``None`` — the pong is queued in the reliability layer.
        """
        bs = BitStream(data)
        bs.read_uint8()  # msg ID
        ping_time = bs.read_int64()

        reply = BitStream()
        reply.write_uint8(ID_CONNECTED_PONG)
        reply.write_int64(ping_time)
        reply.write_int64(int(now * 1000))
        self._reliability.send(reply.get_data(), Reliability.UNRELIABLE)
        return None

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Initiate a graceful disconnect.

        Sends ``ID_DISCONNECTION_NOTIFICATION`` and transitions to the
        ``DISCONNECTING`` state.
        """
        if self.state == ConnectionState.CONNECTED:
            bs = BitStream()
            bs.write_uint8(ID_DISCONNECTION_NOTIFICATION)
            self._reliability.send(bs.get_data(), Reliability.RELIABLE_ORDERED)
            self.state = ConnectionState.DISCONNECTING
