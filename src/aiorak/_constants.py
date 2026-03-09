"""Protocol constants, message IDs, and magic bytes for RakNet wire compatibility.

This module defines all numeric constants required by the RakNet reliable UDP
protocol.  Values are drawn directly from the C++ reference implementation
(``RakNetVersion.h``, ``MessageIdentifiers.h``, ``MTUSize.h``, and
``ReliabilityLayer.cpp``) so that encoded datagrams are wire-compatible with
standard RakNet 4.x peers.
"""

# ---------------------------------------------------------------------------
# Offline message magic (16 bytes) — used to distinguish offline handshake
# packets from regular connected traffic.
# Defined in RakPeer.cpp as OFFLINE_MESSAGE_DATA_ID.
# ---------------------------------------------------------------------------
OFFLINE_MAGIC: bytes = bytes([
    0x00, 0xFF, 0xFF, 0x00,
    0xFE, 0xFE, 0xFE, 0xFE,
    0xFD, 0xFD, 0xFD, 0xFD,
    0x12, 0x34, 0x56, 0x78,
])

# ---------------------------------------------------------------------------
# Protocol version — must match the remote peer or the connection is rejected
# with ``ID_INCOMPATIBLE_PROTOCOL_VERSION``.
# ---------------------------------------------------------------------------
RAKNET_PROTOCOL_VERSION: int = 6

# ---------------------------------------------------------------------------
# MTU constants
# ---------------------------------------------------------------------------
MAXIMUM_MTU: int = 1492
"""Largest MTU attempted during the connection handshake."""

MINIMUM_MTU: int = 400
"""Smallest MTU the protocol will accept."""

UDP_HEADER_SIZE: int = 28
"""IPv4 header (20 B) + UDP header (8 B) overhead subtracted from the MTU."""

MTU_DISCOVERY_SIZES: tuple[int, ...] = (1492, 1200, 576)
"""MTU sizes attempted in order during client connection handshake."""

# ---------------------------------------------------------------------------
# Reliability layer limits
# ---------------------------------------------------------------------------
NUMBER_OF_ORDERED_STREAMS: int = 32
"""Maximum number of independent ordering channels."""

RESEND_BUFFER_SIZE: int = 512
"""Default size of the datagram resend buffer (ring buffer slots)."""

# ---------------------------------------------------------------------------
# Sequence number space — 24-bit unsigned integers
# ---------------------------------------------------------------------------
SEQ_NUM_BITS: int = 24
SEQ_NUM_MAX: int = 0xFFFFFF
"""Largest valid 24-bit sequence / datagram number."""

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT: float = 10.0
"""Seconds of silence before a connection is considered lost."""

SYN_INTERVAL: float = 0.010
"""Base tick interval (10 ms) used by the congestion controller and the
update loop for sending ACKs."""

# ---------------------------------------------------------------------------
# Message IDs — first byte of every offline or reliable message.
# Values taken from the ``DefaultMessageIDTypes`` enum in
# ``MessageIdentifiers.h`` (C++ RakNet 4.081).
# ---------------------------------------------------------------------------

# Internal connected messages
ID_CONNECTED_PING: int = 0
ID_UNCONNECTED_PING: int = 1
ID_UNCONNECTED_PING_OPEN_CONNECTIONS: int = 2
ID_CONNECTED_PONG: int = 3
ID_DETECT_LOST_CONNECTIONS: int = 4

# Offline handshake sequence
ID_OPEN_CONNECTION_REQUEST_1: int = 5
ID_OPEN_CONNECTION_REPLY_1: int = 6
ID_OPEN_CONNECTION_REQUEST_2: int = 7
ID_OPEN_CONNECTION_REPLY_2: int = 8

# Reliable handshake (sent over the reliability layer)
ID_CONNECTION_REQUEST: int = 9

# Internal IDs 10–15 are reserved / security-related and unused here.

# User-visible connection events (returned to application layer)
ID_CONNECTION_REQUEST_ACCEPTED: int = 16
ID_CONNECTION_ATTEMPT_FAILED: int = 17
ID_ALREADY_CONNECTED: int = 18
ID_NEW_INCOMING_CONNECTION: int = 19
ID_NO_FREE_INCOMING_CONNECTIONS: int = 20
ID_DISCONNECTION_NOTIFICATION: int = 21
ID_CONNECTION_LOST: int = 22
ID_CONNECTION_BANNED: int = 23
ID_INVALID_PASSWORD: int = 24
ID_INCOMPATIBLE_PROTOCOL_VERSION: int = 25
ID_IP_RECENTLY_CONNECTED: int = 26
ID_TIMESTAMP: int = 27
ID_UNCONNECTED_PONG: int = 28

# Application messages should start at or above this value.
ID_USER_PACKET_ENUM: int = 134
