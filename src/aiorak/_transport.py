"""asyncio DatagramProtocol wrapper for RakNet UDP I/O.

This module provides a thin abstraction over :mod:`asyncio`'s datagram
transport so that the rest of the library can send and receive UDP datagrams
without coupling directly to the event loop.

Classes:
    :class:`RakNetTransport` — ``asyncio.DatagramProtocol`` subclass that
        dispatches received datagrams to a callback.
    :class:`UDPSocket` — convenience wrapper around the asyncio transport
        for sending datagrams.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional


class RakNetTransport(asyncio.DatagramProtocol):
    """asyncio datagram protocol that forwards packets to a callback.

    When a UDP datagram arrives, :meth:`datagram_received` invokes the
    user-supplied *on_datagram* callback with the raw bytes and the sender
    address.

    Args:
        on_datagram: Callable invoked as ``on_datagram(data, addr)`` for
            every received UDP datagram.
    """

    def __init__(
        self,
        on_datagram: Callable[[bytes, tuple[str, int]], None],
    ) -> None:
        self._on_datagram = on_datagram
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Called when the UDP socket is ready.

        Stores a reference to the transport for sending.

        Args:
            transport: The asyncio datagram transport.
        """
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Called when a UDP datagram is received.

        Args:
            data: Raw datagram bytes.
            addr: ``(host, port)`` of the sender.
        """
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        """Called when a send/receive error occurs.

        Logs the error but does not tear down the transport — transient UDP
        errors (e.g. ICMP unreachable) are expected.

        Args:
            exc: The exception that occurred.
        """
        # Transient UDP errors are normal; do not close the transport.
        pass

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """Called when the transport is closed.

        Args:
            exc: The exception that caused the loss, or ``None`` for a clean
                shutdown.
        """
        self._transport = None


class UDPSocket:
    """Convenience wrapper around an asyncio datagram transport.

    Provides a simple :meth:`send_to` method and exposes the local bound
    address.

    Args:
        transport: The asyncio datagram transport obtained from
            :meth:`asyncio.loop.create_datagram_endpoint`.
    """

    __slots__ = ("_transport",)

    def __init__(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def send_to(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send a UDP datagram to the specified address.

        Args:
            data: Raw bytes to send.
            addr: ``(host, port)`` destination.
        """
        self._transport.sendto(data, addr)

    @property
    def local_address(self) -> tuple[str, int]:
        """The local ``(host, port)`` this socket is bound to.

        Returns:
            A 2-tuple of the local address.  If the socket was bound to port 0,
            this reflects the OS-assigned port.
        """
        sockname = self._transport.get_extra_info("sockname")
        return (sockname[0], sockname[1])

    def close(self) -> None:
        """Close the underlying transport."""
        self._transport.close()
