"""RakNet client: connect to a server and exchange messages.

The :class:`Client` class creates a UDP socket, performs MTU discovery and
the full RakNet connection handshake, and then provides ``async for``
iteration over incoming data packets as raw bytes.

Example::

    client = await aiorak.connect(('127.0.0.1', 19132))
    await client.send(b"hello")
    async for data in client:
        print("Got:", data)
"""

import asyncio
import logging
import random
import time as _time
from collections.abc import AsyncIterator

from ._connection import Connection, ConnectionState, _Signal
from ._constants import MAXIMUM_MTU, MINIMUM_MTU, NUMBER_OF_INTERNAL_IDS, RAKNET_PROTOCOL_VERSION
from ._transport import RakNetTransport, UDPSocket
from ._types import Reliability

logger = logging.getLogger(__name__)


class Client:
    """RakNet-compatible UDP client.

    Manages a single server connection and provides an async interface for
    sending and receiving messages.  Always create via :func:`aiorak.connect`.

    Args:
        server_address: ``(host, port)`` of the server to connect to.
        guid: 64-bit client GUID.  Generated randomly if not supplied.
        protocol_version: RakNet protocol version for handshake validation.
        max_mtu: Largest MTU accepted during handshake.
        min_mtu: Smallest MTU accepted during handshake.
        mtu_discovery_sizes: MTU sizes attempted in order during connection
            handshake.  Defaults to ``(max_mtu, 1200, 576)``.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        guid: int | None = None,
        protocol_version: int = RAKNET_PROTOCOL_VERSION,
        max_mtu: int = MAXIMUM_MTU,
        min_mtu: int = MINIMUM_MTU,
        mtu_discovery_sizes: tuple[int, ...] | None = None,
        num_internal_ids: int = NUMBER_OF_INTERNAL_IDS,
    ) -> None:
        self._server_address = server_address
        self._guid = guid if guid is not None else random.getrandbits(64)
        self._protocol_version = protocol_version
        self._max_mtu = max_mtu
        self._min_mtu = min_mtu
        self._mtu_discovery_sizes = mtu_discovery_sizes if mtu_discovery_sizes is not None else (max_mtu, 1200, 576)
        self._num_internal_ids = num_internal_ids

        # Set by connect(); always non-None after successful connect().
        self._connection: Connection
        self._socket: UDPSocket
        self._update_task: asyncio.Task[None]
        self._connected_event: asyncio.Event = asyncio.Event()
        self._connect_error: OSError | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, timeout: float = 10.0) -> None:
        """Open a UDP socket and perform the RakNet handshake.

        Args:
            timeout: Maximum seconds to wait for the handshake to complete.

        Raises:
            asyncio.TimeoutError: If the handshake does not complete in time.
            OSError: If the socket cannot be created.
        """
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: RakNetTransport(self._on_datagram, self._on_transport_error),
            remote_addr=self._server_address,
        )
        self._socket = UDPSocket(transport)

        # Use the resolved peer address (IP, port) from the transport so that
        # write_address() in handshake packets gets a dotted-quad IP, not a hostname.
        peer_addr = transport.get_extra_info("peername")
        resolved_address = (peer_addr[0], peer_addr[1]) if peer_addr else self._server_address

        self._connection = Connection(
            address=resolved_address,
            guid=self._guid,
            is_server=False,
            mtu=self._max_mtu,
            timeout=timeout,
            protocol_version=self._protocol_version,
            max_mtu=self._max_mtu,
            min_mtu=self._min_mtu,
            mtu_discovery_sizes=self._mtu_discovery_sizes,
            num_internal_ids=self._num_internal_ids,
        )
        self._connection._local_port = self._socket.local_address[1]

        now = _time.monotonic()
        initial_pkt = self._connection.start_connect(now)
        self._send_raw(initial_pkt, self._server_address)

        self._update_task = asyncio.create_task(self._update_loop())

        # Wait for connection to be established
        await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        if self._connect_error is not None:
            raise self._connect_error
        logger.debug("Connected to %s", self._server_address)

    async def close(self, *, notify: bool = True) -> None:
        """Disconnect from the server and release resources.

        Args:
            notify: If ``True`` (default), send ``ID_DISCONNECTION_NOTIFICATION``
                so the server detects the disconnect immediately.  If ``False``,
                silently drop — the server will detect it via timeout.
        """
        if self._closed:
            return
        self._closed = True

        if notify:
            self._connection.disconnect()

        # Let the update loop flush the disconnect notification.
        if notify:
            try:
                await asyncio.wait_for(self._drain(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        self._update_task.cancel()
        try:
            await self._update_task
        except asyncio.CancelledError:
            pass

        self._socket.close()

        # Unblock any waiting iterator
        self._connection._feed_disconnect()

    async def _drain(self) -> None:
        """Wait until the reliability layer has no pending outgoing data."""
        while self._connection.has_pending_data and self._connection.state == ConnectionState.DISCONNECTING:
            await asyncio.sleep(0.01)

    @property
    def is_connected(self) -> bool:
        """``True`` if the client is fully connected to the server."""
        return self._connection.state == ConnectionState.CONNECTED

    @property
    def local_address(self) -> tuple[str, int]:
        """The local ``(host, port)`` the client socket is bound to."""
        return self._socket.local_address

    @property
    def timeout(self) -> float:
        """Dead-connection timeout in seconds.

        Matches C++ ``SetTimeoutTime()`` / ``GetTimeoutTime()``
        (``RakPeer.cpp:2524-2546``).  Separate from the handshake timeout
        passed to :meth:`connect`.
        """
        return self._connection.timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._connection.timeout = value

    @property
    def mtu(self) -> int:
        """The negotiated MTU for this connection.

        Matches C++ ``RakPeer::GetMTUSize()`` (``RakPeer.cpp:2572``).
        """
        return self._connection.mtu

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
    ) -> None:
        """Send a message to the server.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            channel: Ordering channel (0-31).

        Raises:
            RuntimeError: If the client is not connected.
        """
        self._connection._send(data, reliability, channel)

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    async def recv(self) -> bytes:
        """Wait for and return the next received packet.

        Returns:
            The raw payload bytes.

        Raises:
            ConnectionError: If the connection was closed.
        """
        return await self._connection.recv()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield received data packets as raw bytes.

        The iterator ends when the connection is closed.
        """
        async for data in self._connection:
            yield data

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_transport_error(self, exc: Exception) -> None:
        """Callback for UDP transport errors (e.g. ICMP unreachable).

        During handshake, this fails the connect fast instead of waiting
        for the full timeout.
        """
        if not self._connected_event.is_set():
            self._connect_error = OSError(str(exc))
            self._connected_event.set()  # unblock connect()

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback for incoming UDP datagrams."""
        now = _time.monotonic()

        responses = self._connection.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, self._server_address)

        # Process signals
        for signal, sig_data in self._connection.poll_events():
            if signal == _Signal.CONNECT:
                self._connected_event.set()
            elif signal == _Signal.DISCONNECT:
                if not self._connected_event.is_set():
                    self._connect_error = ConnectionRefusedError(
                        f"Connection rejected by server (ID {sig_data[0] if sig_data else '?'})"
                    )
                    self._connected_event.set()
                self._connection._feed_disconnect()
                self._closed = True
            elif signal == _Signal.RECEIVE:
                self._connection._feed_data(sig_data)

    def _send_raw(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send raw bytes over the UDP socket."""
        self._socket.send_to(data, addr)

    # ------------------------------------------------------------------
    # Internal: update loop
    # ------------------------------------------------------------------

    async def _update_loop(self) -> None:
        """Background task that ticks the connection every ~10 ms."""
        try:
            while True:
                # Stop if closed AND no pending data to flush
                if self._closed and not self._connection.has_pending_data:
                    break

                now = _time.monotonic()
                datagrams = self._connection.update(now)
                for dg in datagrams:
                    self._send_raw(dg, self._server_address)

                # Process signals
                for signal, sig_data in self._connection.poll_events():
                    if signal == _Signal.CONNECT:
                        self._connected_event.set()
                    elif signal == _Signal.DISCONNECT:
                        self._connection._feed_disconnect()
                        self._closed = True
                    elif signal == _Signal.RECEIVE:
                        self._connection._feed_data(sig_data)

                # Check for completed reliable messages
                while True:
                    msg = self._connection.poll_receive()
                    if msg is None:
                        break
                    data_bytes, channel = msg
                    if data_bytes and self._connection.state == ConnectionState.CONNECTED:
                        self._connection._feed_data(data_bytes)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
