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
import random
import time as _time
from collections.abc import AsyncIterator

from ._connection import Connection, ConnectionState, _Signal
from ._constants import MAXIMUM_MTU
from ._transport import RakNetTransport, UDPSocket
from ._types import Reliability


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
        guid: int | None = None,
    ) -> None:
        self._server_address = server_address
        self._guid = guid if guid is not None else random.getrandbits(64)

        self._connection: Connection | None = None
        self._socket: UDPSocket | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._connected_event: asyncio.Event = asyncio.Event()
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
        if self._closed:
            return
        self._closed = True

        if self._connection is not None:
            self._connection.disconnect()

        # Let the update loop flush the disconnect notification.
        if self._connection is not None and self._update_task is not None:
            try:
                await asyncio.wait_for(self._drain(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if self._update_task is not None:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        if self._socket is not None:
            self._socket.close()

        # Unblock any waiting iterator
        if self._connection is not None:
            self._connection._feed_disconnect()

    async def _drain(self) -> None:
        """Wait until the reliability layer has no pending outgoing data."""
        while (
            self._connection is not None
            and self._connection.has_pending_data
            and self._connection.state == ConnectionState.DISCONNECTING
        ):
            await asyncio.sleep(0.01)

    @property
    def is_connected(self) -> bool:
        """``True`` if the client is fully connected to the server."""
        return self._connection is not None and self._connection.state == ConnectionState.CONNECTED

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
            channel: Ordering channel (0-31).

        Raises:
            RuntimeError: If the client is not connected.
        """
        if self._connection is None:
            raise RuntimeError("Client is not connected")
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
        if self._connection is None:
            raise ConnectionError("Client is not connected")
        return await self._connection.recv()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield received data packets as raw bytes.

        The iterator ends when the connection is closed.
        """
        if self._connection is None:
            return
        async for data in self._connection:
            yield data

    # ------------------------------------------------------------------
    # Internal: datagram dispatch
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Callback for incoming UDP datagrams."""
        if self._connection is None:
            return
        now = _time.monotonic()

        responses = self._connection.on_datagram(data, now)
        for resp in responses:
            self._send_raw(resp, self._server_address)

        # Process signals
        for signal, sig_data in self._connection.poll_events():
            if signal == _Signal.CONNECT:
                self._connected_event.set()
            elif signal == _Signal.DISCONNECT:
                self._connection._feed_disconnect()
                self._closed = True
            elif signal == _Signal.RECEIVE:
                self._connection._feed_data(sig_data)

    def _send_raw(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send raw bytes over the UDP socket."""
        if self._socket is not None:
            self._socket.send_to(data, addr)

    # ------------------------------------------------------------------
    # Internal: update loop
    # ------------------------------------------------------------------

    async def _update_loop(self) -> None:
        """Background task that ticks the connection every ~10 ms."""
        try:
            while True:
                if self._connection is None:
                    break
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
