"""Custom exception hierarchy for aiorak.

All aiorak-specific exceptions inherit from :class:`RakNetError`, making it
easy to catch any library error with a single ``except`` clause while still
allowing fine-grained handling of specific failure modes.
"""


class RakNetError(Exception):
    """Base exception for all aiorak errors."""


class HandshakeError(RakNetError):
    """The connection handshake failed.

    Raised when the RakNet handshake cannot complete — for example, due to a
    protocol version mismatch, the server being full, or the server rejecting
    the connection for any other reason.
    """


class ConnectionClosedError(RakNetError):
    """An operation was attempted on a closed connection.

    Raised when calling :meth:`~Connection.send` or similar methods after the
    connection has already been closed or disconnected.
    """


class ProtocolError(RakNetError):
    """A wire-protocol violation was detected.

    Raised when a received packet cannot be decoded or violates the RakNet
    protocol in a way that prevents further processing.
    """
