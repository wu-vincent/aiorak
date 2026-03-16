"""Unit tests for wire format encode/decode round-trips."""

import pytest

from aiorak._bitstream import BitStream
from aiorak._constants import DATAGRAM_MESSAGE_ID_ARRAY_LENGTH
from aiorak._types import Reliability
from aiorak._wire import (
    MessageFrame,
    decode_datagram,
    decode_message_frame,
    decode_range_list,
    encode_ack,
    encode_datagram,
    encode_message_frame,
    encode_nak,
    encode_range_list,
)


class TestRangeList:
    def test_range_list_single_value(self):
        """Single-element range (5,5) round-trips correctly."""
        bs = BitStream()
        encode_range_list(bs, [(5, 5)])
        bs.reset_read()
        assert decode_range_list(bs) == [(5, 5)]

    def test_range_list_range(self):
        """Span range (1,5) round-trips correctly."""
        bs = BitStream()
        encode_range_list(bs, [(1, 5)])
        bs.reset_read()
        assert decode_range_list(bs) == [(1, 5)]

    def test_range_list_mixed(self):
        """Mixed singleton and span ranges round-trip correctly."""
        ranges = [(1, 1), (3, 7), (10, 10)]
        bs = BitStream()
        encode_range_list(bs, ranges)
        bs.reset_read()
        assert decode_range_list(bs) == ranges

    def test_range_list_empty(self):
        """Empty range list round-trips to empty list."""
        bs = BitStream()
        encode_range_list(bs, [])
        bs.reset_read()
        assert decode_range_list(bs) == []


class TestMessageFrame:
    def test_unreliable_round_trip(self):
        """UNRELIABLE frame round-trips with correct data and default fields."""
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"\x86hello")
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.reliability == Reliability.UNRELIABLE
        assert decoded.data == b"\x86hello"
        assert decoded.reliable_message_number == 0
        assert decoded.split_packet_count == 0

    def test_reliable_ordered_round_trip(self):
        """RELIABLE_ORDERED frame preserves all fields through encode/decode."""
        frame = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"\x86test",
            reliable_message_number=42,
            ordering_index=7,
            ordering_channel=3,
        )
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.reliability == Reliability.RELIABLE_ORDERED
        assert decoded.data == b"\x86test"
        assert decoded.reliable_message_number == 42
        assert decoded.ordering_index == 7
        assert decoded.ordering_channel == 3

    def test_split_round_trip(self):
        """Split frame preserves split_packet_count, split_packet_id, and split_packet_index."""
        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"\x86chunk",
            reliable_message_number=10,
            split_packet_count=3,
            split_packet_id=1,
            split_packet_index=0,
        )
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.split_packet_count == 3
        assert decoded.split_packet_id == 1
        assert decoded.split_packet_index == 0

    def test_sequenced_round_trip(self):
        """UNRELIABLE_SEQUENCED frame preserves sequencing and ordering fields."""
        frame = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"\x86seq",
            sequencing_index=5,
            ordering_index=2,
            ordering_channel=1,
        )
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.reliability == Reliability.UNRELIABLE_SEQUENCED
        assert decoded.sequencing_index == 5
        assert decoded.ordering_index == 2
        assert decoded.ordering_channel == 1


class TestDatagram:
    def test_datagram_data_round_trip(self):
        """Data datagram with two frames round-trips with correct header and frame data."""
        frames = [
            MessageFrame(reliability=Reliability.UNRELIABLE, data=b"\x86a"),
            MessageFrame(reliability=Reliability.RELIABLE, data=b"\x86b", reliable_message_number=0),
        ]
        raw = encode_datagram(42, frames)
        header, decoded_frames = decode_datagram(raw)
        assert header.is_data is True
        assert header.datagram_number == 42
        assert len(decoded_frames) == 2
        assert decoded_frames[0].data == b"\x86a"
        assert decoded_frames[1].data == b"\x86b"

    def test_datagram_ack_round_trip(self):
        """ACK datagram round-trips with correct ranges."""
        raw = encode_ack([(0, 5), (10, 10)])
        header, ranges = decode_datagram(raw)
        assert header.is_ack is True
        assert ranges == [(0, 5), (10, 10)]

    def test_datagram_nak_round_trip(self):
        """NAK datagram round-trips with correct ranges."""
        raw = encode_nak([(3, 7)])
        header, ranges = decode_datagram(raw)
        assert header.is_nak is True
        assert ranges == [(3, 7)]

    def test_datagram_header_flags(self):
        """Data datagram preserves is_continuous_send, needs_b_and_as, and is_packet_pair flags."""
        raw = encode_datagram(
            0,
            [MessageFrame(reliability=Reliability.UNRELIABLE, data=b"\x86x")],
            is_continuous_send=True,
            needs_b_and_as=True,
            is_packet_pair=True,
        )
        header, _ = decode_datagram(raw)
        assert header.is_data is True
        assert header.is_continuous_send is True
        assert header.needs_b_and_as is True
        assert header.is_packet_pair is True

    def test_ack_with_b_and_as(self):
        """ACK datagram with has_b_and_as flag preserves the flag through round-trip."""
        raw = encode_ack([(1, 1)], has_b_and_as=True)
        header, ranges = decode_datagram(raw)
        assert header.is_ack is True
        assert header.has_b_and_as is True
        assert ranges == [(1, 1)]


class TestRangeListWireLayout:
    def test_range_list_byte_layout(self):
        """Single-value range list is exactly 6 bytes on wire."""
        bs = BitStream()
        encode_range_list(bs, [(5, 5)])
        assert bs.get_byte_length() == 6
        bs.reset_read()
        assert decode_range_list(bs) == [(5, 5)]

    def test_range_list_with_range(self):
        """Span range list (1,10) is exactly 9 bytes on wire."""
        bs = BitStream()
        encode_range_list(bs, [(1, 10)])
        assert bs.get_byte_length() == 9
        bs.reset_read()
        assert decode_range_list(bs) == [(1, 10)]


class TestUnalignedPayload:
    def test_unreliable_frame_unaligned_payload(self):
        """UNRELIABLE frames use unaligned payload path and round-trip correctly."""
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"\x86hello world!")
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.data == b"\x86hello world!"

    def test_reliable_ordered_frame_data_integrity(self):
        """RELIABLE_ORDERED frame encode/decode preserves all fields and data."""
        frame = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"\x86payload bytes here",
            reliable_message_number=100,
            ordering_index=42,
            ordering_channel=5,
        )
        bs = BitStream()
        encode_message_frame(bs, frame)
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.reliability == Reliability.RELIABLE_ORDERED
        assert decoded.data == b"\x86payload bytes here"
        assert decoded.reliable_message_number == 100
        assert decoded.ordering_index == 42
        assert decoded.ordering_channel == 5


class TestPostDecodeValidation:
    """Malformed frames are rejected by post-decode validation."""

    def test_zero_data_bit_length_rejected(self):
        """Frame with dataBitLength == 0 returns None.

        Enough padding is added to pass the minimum 32-bit size check so the
        validation at line 250 (data_bit_length == 0) is actually reached.
        """
        bs = BitStream()
        bs.write_bits(0, 3)
        bs.write_bit(False)
        bs.align_write_to_byte()
        bs.write_uint16(0)  # data_bit_length = 0
        bs.write_uint16(0)  # padding to pass unread_bits >= 32 check
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_ordering_channel_32_rejected(self):
        """Frame with orderingChannel >= 32 returns None."""
        bs = BitStream()
        bs.write_bits(3, 3)  # RELIABLE_ORDERED
        bs.write_bit(False)
        bs.align_write_to_byte()
        bs.write_uint16(8)
        bs.write_uint24(0)
        bs.align_write_to_byte()
        bs.write_uint24(0)
        bs.write_uint8(32)  # invalid channel
        bs.write_bytes(b"\x00")
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_split_index_ge_count_rejected(self):
        """Frame with splitPacketIndex >= splitPacketCount returns None."""
        bs = BitStream()
        bs.write_bits(2, 3)  # RELIABLE
        bs.write_bit(True)
        bs.align_write_to_byte()
        bs.write_uint16(8)
        bs.write_uint24(0)
        bs.align_write_to_byte()
        bs.write_uint32(3)
        bs.write_uint16(1)
        bs.write_uint32(3)  # index == count (invalid)
        bs.write_bytes(b"\x00")
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_valid_split_accepted(self):
        """Frame with splitPacketIndex < splitPacketCount is accepted."""
        bs = BitStream()
        bs.write_bits(2, 3)
        bs.write_bit(True)
        bs.align_write_to_byte()
        bs.write_uint16(8)
        bs.write_uint24(0)
        bs.align_write_to_byte()
        bs.write_uint32(3)
        bs.write_uint16(1)
        bs.write_uint32(2)  # index < count (valid)
        bs.write_bytes(b"\x00")
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.split_packet_index == 2


class TestDecodeFrameMinSize:
    def test_frame_too_short_returns_none(self):
        """Frame with fewer than 32 unread bits returns None immediately."""
        bs = BitStream()
        bs.write_bits(0, 3)
        bs.write_bit(False)
        bs.align_write_to_byte()
        # Only 8 bits total — well under the 32-bit minimum
        bs.reset_read()
        assert decode_message_frame(bs) is None


class TestWireEdgeCases:
    """Edge cases for wire encoding/decoding."""

    def test_encode_ack_receipt_reliability_variants(self):
        """ACK_RECEIPT reliability variants encode and decode without error."""
        for rel in [
            Reliability.UNRELIABLE_WITH_ACK_RECEIPT,
            Reliability.RELIABLE_WITH_ACK_RECEIPT,
            Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT,
        ]:
            frame = MessageFrame(reliability=rel, data=b"\x42")
            bs = BitStream()
            encode_message_frame(bs, frame)
            bs2 = BitStream(bs.get_data())
            decoded = decode_message_frame(bs2)
            assert decoded is not None
            assert decoded.data == b"\x42"

    def test_decode_datagram_is_valid_zero(self):
        """Datagram with isValid=0 raises ValueError."""
        data = bytes([0x00, 0x00, 0x00, 0x00])
        with pytest.raises(ValueError, match="isValid"):
            decode_datagram(data)

    def test_decode_datagram_truncated_frame(self):
        """Valid data datagram header with truncated frame data yields empty frames list."""
        bs = BitStream()
        bs.write_bit(True)  # isValid
        bs.write_bit(False)  # isACK
        bs.write_bit(False)  # isNAK
        bs.write_bit(False)  # isPacketPair
        bs.write_bit(False)  # isContinuousSend
        bs.write_bit(False)  # needsBAndAs
        bs.align_write_to_byte()
        bs.write_uint24(0)  # datagram number
        bs.write_uint8(0xFF)  # partial frame data
        header, frames = decode_datagram(bs.get_data())
        assert header.is_data
        assert frames == []


class TestRangeListValidation:
    """Range list count validation against DATAGRAM_MESSAGE_ID_ARRAY_LENGTH."""

    def test_valid_range_list(self):
        """Range list with count=3 decodes correctly."""
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(3)
        for i in range(3):
            bs.write_uint8(1)  # min == max
            bs.write_uint24(i)
        bs2 = BitStream(bs.get_data())
        result = decode_range_list(bs2)
        assert len(result) == 3

    def test_exceeds_max_count(self):
        """Range list with count exceeding maximum raises ValueError."""
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH + 1)
        bs2 = BitStream(bs.get_data())
        with pytest.raises(ValueError, match="Range list count"):
            decode_range_list(bs2)

    def test_at_max_count(self):
        """Range list with count exactly at the limit is accepted."""
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH)
        for i in range(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH):
            bs.write_uint8(1)
            bs.write_uint24(i)
        bs2 = BitStream(bs.get_data())
        result = decode_range_list(bs2)
        assert len(result) == DATAGRAM_MESSAGE_ID_ARRAY_LENGTH
