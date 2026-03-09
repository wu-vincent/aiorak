"""RakNet server: accept connections, manage peers, dispatch events.

The :class:`Server` class binds a UDP socket, handles the offline connection
handshake for incoming peers, and manages a collection of
:class:`~aiorak._connection.Connection` instances.  Application code
iterates over a ``Server`` with ``async for`` to receive
:class:`~aiorak._types.Event` objects.

Example::

    server = await aiorak.create_server(('0.0.0.0', 19132))
    async for event in server:
        if event.type == aiorak.EventType.RECEIVE:
            await server.send(event.address, b"reply")
"""

from __future__ import annotations

import asyncio
import random
import time as _time
from typing import AsyncIterator, Optional

from ._connection import Connection, ConnectionState
from ._bitstream import BitStream
from ._constants import (
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    ID_UNCONNECTED_PING,
    ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
    ID_UNCONNECTED_PONG,
    MAXIMUM_MTU,
    OFFLINE_MAGIC,
)
from ._transport import RakNetTransport, UDPSocket
from ._types import Event, EventType, Reliability


class Server:
    """RakNet-compatible UDP server.

    Manages incoming connections and provides an async iterator interface
    for receiving application-level events.

    Args:
        local_address: ``(host, port)`` to bind the UDP socket to.
        max_connections: Maximum number of simultaneous peer connections.
        guid: 64-bit server GUID.  Generated randomly if not supplied.
    """

    def __init__(
        self,
        local_address: tuple[str, int],
        max_connections: int = 64,
        guid: Optional[int] = None,
    ) -> None:
        self._local_address = local_address
        self._max_connections = max_connections
        self._guid = guid if guid is not None else random.getrandbits(64)

        self._connections: dict[tuple[str, int], Connection] = {}
        self._socket: Optional[UDPSocket] = None
        self._event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self._update_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._offline_ping_response: bytes = b""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind the UDP socket and start the background update loop.

        This is called automatically by :func:`aiorak.create_server`.

        Raises:
            OSError: If the address is already in use or otherwise unavailable.
        """
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: RakNetTransport(self._on_datagram),
            local_addr=self._local_address,
        )
        self._socket = UDPSocket(transport)
        self._update_task = asyncio.create_task(self._update_loop())

    async def close(self) -> None:
        """Gracefully shut down the server.

        Disconnects all peers, cancels the update loop, and closes the
        UDP socket.
        """
        self._closed = True
        if self._update_task is not None:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        # Disconnect all peers
        for conn in list(self._connections.values()):
            conn.disconnect()
        if self._socket is not None:
            self._socket.close()
        # Unblock any waiting iterator
        await self._event_queue.put(Event(type=EventType.DISCONNECT, address=("", 0)))

    @property
    def local_address(self) -> tuple[str, int]:
        """The actual ``(host, port)`` the server socket is bound to.

        Useful when binding to port 0 to discover the OS-assigned port.
        """
        if self._socket is not None:
            return self._socket.local_address
        return self._local_address

    # ------------------------------------------------------------------
    # Offline ping
    # ------------------------------------------------------------------

    def set_offline_ping_response(self, data: bytes) -> None:
        """Set custom data to include in unconnected pong replies.

        This data is appended to every ``ID_UNCONNECTED_PONG`` response,
        allowing LAN server discovery or pre-connection metadata exchange.

        Args:
            data: Arbitrary bytes to attach (may be empty).
        """
        self._offline_ping_response = data

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        address: tuple[str, int],
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        channel: int = 0,
    ) -> None:
        """Send a message to a connected peer.

        Args:
            address: ``(host, port)`` of the destination peer.
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            channel: Ordering channel (0–31).

        Raises:
            KeyError: If no connection exists for *address*.
        """
        conn = self._connections.get(address)
        if conn is None:
            raise KeyError(f"No connection for {address}")
        conn.send(data, reliability, channel)

    # ------------------------------------------------------------------
    # Async iterator
    # ------------------------------------------------------------------

    async def __aiter__(self) -> AsyncIterator[Event]:
        """Yield :class:`Event` objects as they occur.

        The iterator runs until :meth:`close` is called.

        Yields:
            Application-level events (connect, disconnect, receive).
        """
        while not self._closed:
            event = await self._event_queue.get()
            if self._closed and event.address == ("", 0):
                break
            yield event

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback invoked by :class:`RakNetTransport` for each UDP datagram.

        Routes the datagram to the appropriate :class:`Connection`, or creates
        a new one for incoming handshake requests.

        Args:
            data: Raw UDP payload.
            addr: ``(host, port)`` of the sender.
        """
        now = _time.monotonic()

        # Check for offline messages from unknown peers
        if addr not in self._connections:
            if len(data) >= 1 and data[0] in (
                ID_UNCONNECTED_PING,
                ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
            ):
                self._handle_unconnected_ping(data, addr)
                return
            if len(data) >= 1 and data[0] == ID_OPEN_CONNECTION_REQUEST_1:
                if len(self._connections) >= self._max_connections:
                    # TODO: send ID_NO_FREE_INCOMING_CONNECTIONS
                    return
                conn = Connection(
                    address=addr,
                    guid=self._guid,
                    is_server=True,
                    mtu=MAXIMUM_MTU,
                )
                conn._system_index = len(self._connections)
                self._connections[addr] = conn

        conn = self._connections.get(addr)
        if conn is None:
            return

        responses = conn.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, addr)

        # Collect events
        for event in conn.poll_events():
            self._event_queue.put_nowait(event)
            if event.type == EventType.DISCONNECT:
                self._connections.pop(addr, None)

    def _handle_unconnected_ping(self, data: bytes, addr: tuple[str, int]) -> None:
        """Respond to an unconnected ping with an unconnected pong.

        Args:
            data: Raw datagram bytes (starts with the ping message ID).
            addr: ``(host, port)`` of the sender.
        """
        # Minimum size: 1 (ID) + 8 (time) + 16 (magic) = 25 bytes
        if len(data) < 25:
            return

        bs = BitStream(data)
        msg_id = bs.read_uint8()
        ping_time = bs.read_int64()
        magic = bs.read_bytes(16)
        if magic != OFFLINE_MAGIC:
            return

        # ID_UNCONNECTED_PING_OPEN_CONNECTIONS: only reply if we have free slots
        if (
            msg_id == ID_UNCONNECTED_PING_OPEN_CONNECTIONS
            and len(self._connections) >= self._max_connections
        ):
            return

        # Build pong response
        pong = BitStream()
        pong.write_uint8(ID_UNCONNECTED_PONG)
        pong.write_int64(ping_time)
        pong.write_uint64(self._guid)
        pong.write_bytes(OFFLINE_MAGIC)
        if self._offline_ping_response:
            pong.write_bytes(self._offline_ping_response)
        self._send_raw(pong.get_data(), addr)

    def _send_raw(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send raw bytes over the UDP socket.

        Args:
            data: Encoded datagram.
            addr: Destination ``(host, port)``.
        """
        if self._socket is not None:
            self._socket.send_to(data, addr)

    # ------------------------------------------------------------------
    # Internal: update loop
    # ------------------------------------------------------------------

    async def _update_loop(self) -> None:
        """Background task that ticks all connections every ~10 ms.

        Flushes ACKs, retransmits expired datagrams, and detects timeouts.
        """
        try:
            while not self._closed:
                now = _time.monotonic()
                dead_addrs: list[tuple[str, int]] = []

                for addr, conn in list(self._connections.items()):
                    datagrams = conn.update(now)
                    for dg in datagrams:
                        self._send_raw(dg, addr)

                    # Collect events
                    for event in conn.poll_events():
                        self._event_queue.put_nowait(event)
                        if event.type == EventType.DISCONNECT:
                            dead_addrs.append(addr)

                    # Check for completed reliable messages
                    while True:
                        msg = conn.poll_receive()
                        if msg is None:
                            break
                        data_bytes, channel = msg
                        if data_bytes and conn.state == ConnectionState.CONNECTED:
                            self._event_queue.put_nowait(Event(
                                type=EventType.RECEIVE,
                                address=addr,
                                data=data_bytes,
                                channel=channel,
                            ))

                for addr in dead_addrs:
                    self._connections.pop(addr, None)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
