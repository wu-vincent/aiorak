"""Enumerations and data classes for the public aiorak API.

This module provides the core type definitions used across the library:

* :class:`Reliability` — packet delivery guarantees (mirrors the C++ enum).
* :class:`Priority` — send-queue priority levels.
* :class:`PingResponse` — result of an offline ping.
"""

import enum
from dataclasses import dataclass


class Reliability(enum.IntEnum):
    """Packet reliability modes matching the C++ ``PacketReliability`` enum.

    The integer values correspond to the 3-bit reliability field written on
    the wire, so they **must not** be changed.
    """

    UNRELIABLE = 0
    """Fire-and-forget.  No ordering, no retry."""

    UNRELIABLE_SEQUENCED = 1
    """Unreliable but sequenced — stale packets are silently dropped."""

    RELIABLE = 2
    """Retransmitted until acknowledged.  No ordering guarantee."""

    RELIABLE_ORDERED = 3
    """Retransmitted and delivered in-order per channel."""

    RELIABLE_SEQUENCED = 4
    """Retransmitted, but only the newest packet in the sequence is kept."""

    UNRELIABLE_WITH_ACK_RECEIPT = 5
    """Same as UNRELIABLE; the sender is notified on ACK or loss."""

    RELIABLE_WITH_ACK_RECEIPT = 6
    """Same as RELIABLE; the sender is notified on ACK."""

    RELIABLE_ORDERED_WITH_ACK_RECEIPT = 7
    """Same as RELIABLE_ORDERED; the sender is notified on ACK."""

    @property
    def is_reliable(self) -> bool:
        """Return ``True`` if this mode requires a reliable message number."""
        return self in (
            Reliability.RELIABLE,
            Reliability.RELIABLE_SEQUENCED,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_WITH_ACK_RECEIPT,
            Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT,
        )

    @property
    def is_sequenced(self) -> bool:
        """Return ``True`` if this mode uses a sequencing index."""
        return self in (Reliability.UNRELIABLE_SEQUENCED, Reliability.RELIABLE_SEQUENCED)

    @property
    def is_ordered(self) -> bool:
        """Return ``True`` if this mode uses an ordering index + channel."""
        return self in (
            Reliability.UNRELIABLE_SEQUENCED,
            Reliability.RELIABLE_SEQUENCED,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT,
        )


class Priority(enum.IntEnum):
    """Send-queue priority levels.

    Higher-priority messages are transmitted before lower-priority ones in the
    same update tick.
    """

    IMMEDIATE = 0
    """Sent before anything else in the current tick."""

    HIGH = 1
    MEDIUM = 2
    LOW = 3


@dataclass(frozen=True, slots=True)
class PingResponse:
    """Response from an unconnected (offline) ping.

    Attributes:
        latency_ms: Round-trip time in milliseconds.
        server_guid: The 64-bit GUID of the responding server.
        data: Custom offline ping response data set by the server, or ``b""``.
        address: ``(host, port)`` of the responding server.
    """

    latency_ms: float
    server_guid: int
    data: bytes
    address: tuple[str, int]
