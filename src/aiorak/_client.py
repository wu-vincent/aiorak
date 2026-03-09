"""RakNet client: initiate a connection and exchange messages with a server.

The :class:`Client` class creates a UDP socket, performs MTU discovery and
the full RakNet connection handshake, and then provides ``async for``
iteration over incoming :class:`~aiorak._types.Event` objects.

Example::

    client = await aiorak.connect(('127.0.0.1', 19132))
    await client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
    async for event in client:
        if event.type == aiorak.EventType.RECEIVE:
            print("Got:", event.data)
"""

from __future__ import annotations

import asyncio
import random
import time as _time
from typing import AsyncIterator, Optional

from ._connection import Connection, ConnectionState
from ._constants import MAXIMUM_MTU
from ._transport import RakNetTransport, UDPSocket
from ._types import Event, EventType, Reliability


class Client:
    """RakNet-compatible UDP client.

    Manages a single server connection and provides an async interface for
    sending and receiving messages.

    Args:
        server_address: ``(host, port)`` of the server to connect to.
        guid: 64-bit client GUID.  Generated randomly if not supplied.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        guid: Optional[int] = None,
    ) -> None:
        self._server_address = server_address
        self._guid = guid if guid is not None else random.getrandbits(64)

        self._connection: Optional[Connection] = None
        self._socket: Optional[UDPSocket] = None
        self._event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self._update_task: Optional[asyncio.Task[None]] = None
        self._connected_event: asyncio.Event = asyncio.Event()
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, timeout: float = 10.0) -> None:
        """Open a UDP socket and perform the RakNet handshake.

        Blocks until the connection is fully established or *timeout*
        seconds elapse.

        Args:
            timeout: Maximum seconds to wait for the handshake to complete.

        Raises:
            asyncio.TimeoutError: If the handshake does not complete in time.
            OSError: If the socket cannot be created.
        """
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: RakNetTransport(self._on_datagram),
            remote_addr=self._server_address,
        )
        self._socket = UDPSocket(transport)

        self._connection = Connection(
            address=self._server_address,
            guid=self._guid,
            is_server=False,
            mtu=MAXIMUM_MTU,
        )

        now = _time.monotonic()
        initial_pkt = self._connection.start_connect(now)
        self._send_raw(initial_pkt, self._server_address)

        self._update_task = asyncio.create_task(self._update_loop())

        # Wait for connection to be established
        await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)

    async def close(self) -> None:
        """Gracefully disconnect from the server and release resources."""
        self._closed = True
        if self._connection is not None:
            self._connection.disconnect()
        if self._update_task is not None:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        if self._socket is not None:
            self._socket.close()
        # Unblock any waiting iterator
        await self._event_queue.put(Event(type=EventType.DISCONNECT, address=self._server_address))

    @property
    def is_connected(self) -> bool:
        """``True`` if the client is fully connected to the server."""
        return (
            self._connection is not None
            and self._connection.state == ConnectionState.CONNECTED
        )

    @property
    def local_address(self) -> tuple[str, int]:
        """The local ``(host, port)`` the client socket is bound to."""
        if self._socket is not None:
            return self._socket.local_address
        return ("0.0.0.0", 0)

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
            channel: Ordering channel (0–31).

        Raises:
            RuntimeError: If the client is not connected.
        """
        if self._connection is None:
            raise RuntimeError("Client is not connected")
        self._connection.send(data, reliability, channel)

    # ------------------------------------------------------------------
    # Async iterator
    # ------------------------------------------------------------------

    async def __aiter__(self) -> AsyncIterator[Event]:
        """Yield :class:`Event` objects as they arrive from the server.

        The iterator runs until the connection is closed.

        Yields:
            Application-level events (connect, disconnect, receive).
        """
        while not self._closed:
            event = await self._event_queue.get()
            if self._closed and event.type == EventType.DISCONNECT:
                yield event
                break
            yield event

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback for incoming UDP datagrams.

        Args:
            data: Raw UDP payload.
            addr: ``(host, port)`` of the sender.
        """
        if self._connection is None:
            return
        now = _time.monotonic()

        responses = self._connection.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, self._server_address)

        # Collect events
        for event in self._connection.poll_events():
            self._event_queue.put_nowait(event)
            if event.type == EventType.CONNECT:
                self._connected_event.set()

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
        """Background task that ticks the connection every ~10 ms.

        Handles ACK/NAK flushing, retransmission, timeout detection, and
        promotes completed reliable messages to the event queue.
        """
        try:
            while not self._closed:
                if self._connection is None:
                    break

                now = _time.monotonic()
                datagrams = self._connection.update(now)
                for dg in datagrams:
                    self._send_raw(dg, self._server_address)

                # Collect events
                for event in self._connection.poll_events():
                    self._event_queue.put_nowait(event)
                    if event.type == EventType.CONNECT:
                        self._connected_event.set()
                    elif event.type == EventType.DISCONNECT:
                        self._closed = True

                # Check for completed reliable messages
                while True:
                    msg = self._connection.poll_receive()
                    if msg is None:
                        break
                    data_bytes, channel = msg
                    if data_bytes and self._connection.state == ConnectionState.CONNECTED:
                        self._event_queue.put_nowait(Event(
                            type=EventType.RECEIVE,
                            address=self._server_address,
                            data=data_bytes,
                            channel=channel,
                        ))

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
