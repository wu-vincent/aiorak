"""Datagram header, message frame, and range list encode/decode.

This module implements the low-level wire format as described in
``ReliabilityLayer.cpp`` (datagram headers at lines 110–227, message frames
at lines 2610–2789) and ``DS_RangeList.h`` (lines 65–144).

All functions operate on :class:`~aiorak._bitstream.BitStream` instances so
that the bit-level alignment rules of the RakNet protocol are respected.

Datagram types
--------------
Every UDP payload begins with a 1-bit *isValid* flag (always ``1``), followed
by a 1-bit *isACK* flag and a 1-bit *isNAK* flag.  Exactly one of the three
resulting categories applies:

* **ACK** — ``isValid=1, isACK=1``: carries a range list of acknowledged
  datagram sequence numbers.
* **NAK** — ``isValid=1, isACK=0, isNAK=1``: carries a range list of missing
  datagram sequence numbers.
* **Data** — ``isValid=1, isACK=0, isNAK=0``: carries a datagram sequence
  number followed by one or more message frames.
"""

import struct
from dataclasses import dataclass, field

from ._bitstream import BitStream
from ._types import Reliability


# ---------------------------------------------------------------------------
# Range list helpers
# ---------------------------------------------------------------------------

def encode_range_list(bs: BitStream, ranges: list[tuple[int, int]]) -> None:
    """Encode a list of ``(min, max)`` inclusive ranges in RakNet format.

    Wire format (from ``DS_RangeList::Serialize``)::

        count        : uint16 (aligned)
        per range:
            minEqualsMax : uint8  (1 = single value, 0 = range)
            min          : uint24 (LE)
            max          : uint24 (LE) — only if minEqualsMax == 0

    Args:
        bs: Target bitstream to write into.
        ranges: Sorted, non-overlapping ``(min, max)`` pairs.
    """
    bs.align_write_to_byte()
    bs.write_uint16(len(ranges))
    for lo, hi in ranges:
        if lo == hi:
            bs.write_uint8(1)
            bs.write_uint24(lo)
        else:
            bs.write_uint8(0)
            bs.write_uint24(lo)
            bs.write_uint24(hi)


def decode_range_list(bs: BitStream) -> list[tuple[int, int]]:
    """Decode a RakNet range list into ``(min, max)`` inclusive pairs.

    Args:
        bs: Source bitstream positioned at the start of a range list.

    Returns:
        A list of ``(min, max)`` integer pairs.

    Raises:
        ValueError: If the stream does not contain enough data.
    """
    bs.align_read_to_byte()
    count = bs.read_uint16()
    ranges: list[tuple[int, int]] = []
    for _ in range(count):
        min_eq_max = bs.read_uint8()
        lo = bs.read_uint24()
        if min_eq_max:
            ranges.append((lo, lo))
        else:
            hi = bs.read_uint24()
            ranges.append((lo, hi))
    return ranges


# ---------------------------------------------------------------------------
# Message frame
# ---------------------------------------------------------------------------

@dataclass
class MessageFrame:
    """A single message packed inside a data datagram.

    This corresponds to the C++ ``InternalPacket`` as serialized by
    ``WriteToBitStreamFromInternalPacket`` / ``CreateInternalPacketFromBitStream``.

    Attributes:
        reliability: Delivery mode (3-bit enum on the wire).
        data: Raw payload bytes.
        data_bit_length: Payload length in *bits* (may not be byte-aligned).
        reliable_message_number: Per-connection auto-incrementing counter,
            present only for reliable modes.
        sequencing_index: Sequencing counter for sequenced modes.
        ordering_index: Ordering counter for ordered/sequenced modes.
        ordering_channel: Channel number (0–31) for ordered/sequenced modes.
        split_packet_count: Total fragments (0 if not split).
        split_packet_id: Identifier shared by all fragments of a split message.
        split_packet_index: Fragment index within the split set.
    """

    reliability: Reliability
    data: bytes
    data_bit_length: int = 0
    reliable_message_number: int = 0
    sequencing_index: int = 0
    ordering_index: int = 0
    ordering_channel: int = 0
    split_packet_count: int = 0
    split_packet_id: int = 0
    split_packet_index: int = 0

    def __post_init__(self) -> None:
        if self.data_bit_length == 0:
            self.data_bit_length = len(self.data) * 8



def encode_message_frame(bs: BitStream, frame: MessageFrame) -> None:
    """Serialize a :class:`MessageFrame` into *bs*.

    The layout exactly follows ``WriteToBitStreamFromInternalPacket`` in
    ``ReliabilityLayer.cpp:2610``.

    Args:
        bs: Destination bitstream.
        frame: The message frame to encode.
    """
    bs.align_write_to_byte()

    # Map ACK_RECEIPT variants to their base reliability on the wire
    wire_rel = frame.reliability
    if wire_rel == Reliability.UNRELIABLE_WITH_ACK_RECEIPT:
        wire_rel = Reliability.UNRELIABLE
    elif wire_rel == Reliability.RELIABLE_WITH_ACK_RECEIPT:
        wire_rel = Reliability.RELIABLE
    elif wire_rel == Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT:
        wire_rel = Reliability.RELIABLE_ORDERED

    # 3 bits: reliability
    bs.write_bits(int(wire_rel), 3)

    # 1 bit: has split packet
    has_split = frame.split_packet_count > 0
    bs.write_bit(has_split)

    # Align then write data bit length as uint16
    bs.align_write_to_byte()
    bs.write_uint16(frame.data_bit_length)

    # Reliable message number (uint24) — only for reliable modes
    if frame.reliability.is_reliable:
        bs.write_uint24(frame.reliable_message_number)

    bs.align_write_to_byte()

    # Sequencing index (uint24) — only for sequenced modes
    if frame.reliability.is_sequenced:
        bs.write_uint24(frame.sequencing_index)

    # Ordering index (uint24) + channel (uint8) — for ordered/sequenced
    if frame.reliability.is_ordered:
        bs.write_uint24(frame.ordering_index)
        bs.write_uint8(frame.ordering_channel)

    # Split packet fields (all byte-aligned)
    if has_split:
        bs.write_uint32(frame.split_packet_count)
        bs.write_uint16(frame.split_packet_id)
        bs.write_uint32(frame.split_packet_index)

    # Payload bytes (aligned)
    bs.write_bytes(frame.data)


def decode_message_frame(bs: BitStream) -> MessageFrame | None:
    """Deserialize a single :class:`MessageFrame` from *bs*.

    The layout exactly follows ``CreateInternalPacketFromBitStream`` in
    ``ReliabilityLayer.cpp:2682``.

    Args:
        bs: Source bitstream positioned at the start of a message frame.

    Returns:
        A :class:`MessageFrame`, or ``None`` if remaining data is insufficient.
    """
    if bs.unread_bits < 32:
        return None

    bs.align_read_to_byte()

    # 3 bits: reliability
    rel_val = bs.read_bits(3)
    reliability = Reliability(rel_val)

    # 1 bit: has split
    has_split = bs.read_bit()

    # Align + uint16 data bit length
    bs.align_read_to_byte()
    data_bit_length = bs.read_uint16()

    # Reliable message number
    reliable_message_number = 0
    if reliability.is_reliable:
        reliable_message_number = bs.read_uint24()

    bs.align_read_to_byte()

    # Sequencing index
    sequencing_index = 0
    if reliability.is_sequenced:
        sequencing_index = bs.read_uint24()

    # Ordering index + channel
    ordering_index = 0
    ordering_channel = 0
    if reliability.is_ordered:
        ordering_index = bs.read_uint24()
        ordering_channel = bs.read_uint8()

    # Split packet info
    split_count = 0
    split_id = 0
    split_index = 0
    if has_split:
        split_count = bs.read_uint32()
        split_id = bs.read_uint16()
        split_index = bs.read_uint32()

    # Payload
    data_byte_length = (data_bit_length + 7) // 8
    data = bs.read_bytes(data_byte_length)

    return MessageFrame(
        reliability=reliability,
        data=data,
        data_bit_length=data_bit_length,
        reliable_message_number=reliable_message_number,
        sequencing_index=sequencing_index,
        ordering_index=ordering_index,
        ordering_channel=ordering_channel,
        split_packet_count=split_count,
        split_packet_id=split_id,
        split_packet_index=split_index,
    )


# ---------------------------------------------------------------------------
# Datagram-level encode / decode
# ---------------------------------------------------------------------------

@dataclass
class DatagramHeader:
    """Parsed header of an incoming UDP datagram.

    Exactly one of :attr:`is_ack`, :attr:`is_nak`, or :attr:`is_data` is
    ``True``.

    Attributes:
        is_ack: This datagram is an ACK packet.
        is_nak: This datagram is a NAK (negative acknowledgement) packet.
        is_data: This datagram carries one or more message frames.
        has_b_and_as: ACK-only flag indicating bandwidth/arrival-rate fields
            are present.
        is_packet_pair: Data-only flag for packet-pair probing.
        is_continuous_send: Data-only flag for congestion controller hint.
        needs_b_and_as: Data-only flag requesting bandwidth info in ACK reply.
        datagram_number: 24-bit sequence number (data datagrams only).
    """

    is_ack: bool = False
    is_nak: bool = False
    is_data: bool = False
    has_b_and_as: bool = False
    is_packet_pair: bool = False
    is_continuous_send: bool = False
    needs_b_and_as: bool = False
    datagram_number: int = 0


def encode_ack(ranges: list[tuple[int, int]], has_b_and_as: bool = False) -> bytes:
    """Build a complete ACK datagram payload.

    Args:
        ranges: Sorted list of ``(min, max)`` acknowledged datagram numbers.
        has_b_and_as: Whether to include bandwidth/arrival-rate data
            (currently always ``False`` in this implementation).

    Returns:
        The encoded datagram as bytes, ready to send over UDP.
    """
    bs = BitStream()
    # isValid = 1
    bs.write_bit(True)
    # isACK = 1
    bs.write_bit(True)
    # hasBAndAS
    bs.write_bit(has_b_and_as)
    bs.align_write_to_byte()
    # No timestamp (INCLUDE_TIMESTAMP_WITH_DATAGRAMS == 0)
    if has_b_and_as:
        # AS as float — not implemented, write 0.0
        bs.write_bytes(struct.pack(">f", 0.0))
    encode_range_list(bs, ranges)
    return bs.get_data()


def encode_nak(ranges: list[tuple[int, int]]) -> bytes:
    """Build a complete NAK datagram payload.

    Args:
        ranges: Sorted list of ``(min, max)`` missing datagram numbers.

    Returns:
        The encoded datagram as bytes, ready to send over UDP.
    """
    bs = BitStream()
    # isValid = 1
    bs.write_bit(True)
    # isACK = 0
    bs.write_bit(False)
    # isNAK = 1
    bs.write_bit(True)
    bs.align_write_to_byte()
    encode_range_list(bs, ranges)
    return bs.get_data()


def encode_datagram(
    datagram_number: int,
    frames: list[MessageFrame],
    *,
    is_continuous_send: bool = False,
    needs_b_and_as: bool = False,
    is_packet_pair: bool = False,
) -> bytes:
    """Build a complete data datagram payload containing one or more frames.

    Args:
        datagram_number: 24-bit sequence number for this datagram.
        frames: Message frames to pack into the datagram.
        is_continuous_send: Hint for congestion controller.
        needs_b_and_as: Request bandwidth info in the ACK reply.
        is_packet_pair: Packet-pair probe flag.

    Returns:
        The encoded datagram as bytes.
    """
    bs = BitStream()
    # isValid = 1
    bs.write_bit(True)
    # isACK = 0
    bs.write_bit(False)
    # isNAK = 0
    bs.write_bit(False)
    # isPacketPair
    bs.write_bit(is_packet_pair)
    # isContinuousSend
    bs.write_bit(is_continuous_send)
    # needsBAndAs
    bs.write_bit(needs_b_and_as)
    bs.align_write_to_byte()
    # No timestamp
    # datagram sequence number (uint24)
    bs.write_uint24(datagram_number)
    # message frames
    for frame in frames:
        encode_message_frame(bs, frame)
    return bs.get_data()


def decode_datagram(data: bytes) -> tuple[DatagramHeader, list[MessageFrame] | list[tuple[int, int]]]:
    """Decode a raw UDP payload into its header and body.

    Returns:
        A 2-tuple of ``(header, body)`` where:

        * For **ACK** or **NAK** datagrams, *body* is a ``list[tuple[int, int]]``
          of acknowledged/missing sequence number ranges.
        * For **data** datagrams, *body* is a ``list[MessageFrame]`` of the
          contained message frames.

    Raises:
        ValueError: If the datagram is too short or has an invalid header.
    """
    bs = BitStream(data)

    is_valid = bs.read_bit()
    if not is_valid:
        raise ValueError("Datagram isValid bit is 0 — likely offline data")

    is_ack = bs.read_bit()
    header = DatagramHeader()

    if is_ack:
        header.is_ack = True
        header.has_b_and_as = bs.read_bit()
        bs.align_read_to_byte()
        if header.has_b_and_as:
            # Read AS float (4 bytes) — discard for now
            bs.read_bytes(4)
        ranges = decode_range_list(bs)
        return header, ranges

    is_nak = bs.read_bit()
    if is_nak:
        header.is_nak = True
        bs.align_read_to_byte()
        ranges = decode_range_list(bs)
        return header, ranges

    # Data datagram
    header.is_data = True
    header.is_packet_pair = bs.read_bit()
    header.is_continuous_send = bs.read_bit()
    header.needs_b_and_as = bs.read_bit()
    bs.align_read_to_byte()
    header.datagram_number = bs.read_uint24()

    frames: list[MessageFrame] = []
    while bs.unread_bits >= 32:
        frame = decode_message_frame(bs)
        if frame is None:
            break
        frames.append(frame)

    return header, frames
