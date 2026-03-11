"""Unit tests for wire format encode/decode round-trips."""

from aiorak._bitstream import BitStream
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
        bs = BitStream()
        encode_range_list(bs, [(5, 5)])
        bs.reset_read()
        assert decode_range_list(bs) == [(5, 5)]

    def test_range_list_range(self):
        bs = BitStream()
        encode_range_list(bs, [(1, 5)])
        bs.reset_read()
        assert decode_range_list(bs) == [(1, 5)]

    def test_range_list_mixed(self):
        ranges = [(1, 1), (3, 7), (10, 10)]
        bs = BitStream()
        encode_range_list(bs, ranges)
        bs.reset_read()
        assert decode_range_list(bs) == ranges

    def test_range_list_empty(self):
        bs = BitStream()
        encode_range_list(bs, [])
        bs.reset_read()
        assert decode_range_list(bs) == []


class TestMessageFrame:
    def test_unreliable_round_trip(self):
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
        raw = encode_ack([(0, 5), (10, 10)])
        header, ranges = decode_datagram(raw)
        assert header.is_ack is True
        assert ranges == [(0, 5), (10, 10)]

    def test_datagram_nak_round_trip(self):
        raw = encode_nak([(3, 7)])
        header, ranges = decode_datagram(raw)
        assert header.is_nak is True
        assert ranges == [(3, 7)]

    def test_datagram_header_flags(self):
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
        raw = encode_ack([(1, 1)], has_b_and_as=True)
        header, ranges = decode_datagram(raw)
        assert header.is_ack is True
        assert header.has_b_and_as is True
        assert ranges == [(1, 1)]


class TestRangeListWireLayout:
    def test_range_list_byte_layout(self):
        """Range list uses aligned uint16 count and uint8 minEqualsMax flag."""
        bs = BitStream()
        encode_range_list(bs, [(5, 5)])
        # uint16(1) = 2 bytes, uint8(1) = 1 byte, uint24(5) = 3 bytes = 6 bytes total
        assert bs.get_byte_length() == 6
        bs.reset_read()
        assert decode_range_list(bs) == [(5, 5)]

    def test_range_list_with_range(self):
        bs = BitStream()
        encode_range_list(bs, [(1, 10)])
        # uint16(1) = 2 bytes, uint8(0) = 1 byte, uint24(1) + uint24(10) = 6 bytes = 9 bytes
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
        """dataBitLength == 0 should return None."""
        bs = BitStream()
        # 3 bits reliability (UNRELIABLE = 0), 1 bit has_split = 0
        bs.write_bits(0, 3)
        bs.write_bit(False)
        bs.align_write_to_byte()
        # data_bit_length = 0
        bs.write_uint16(0)
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_ordering_channel_32_rejected(self):
        """orderingChannel >= 32 should return None."""
        bs = BitStream()
        # RELIABLE_ORDERED = 3
        bs.write_bits(3, 3)
        bs.write_bit(False)  # no split
        bs.align_write_to_byte()
        bs.write_uint16(8)  # 1 byte of data
        bs.write_uint24(0)  # reliable_message_number
        bs.align_write_to_byte()
        bs.write_uint24(0)  # ordering_index
        bs.write_uint8(32)  # ordering_channel = 32 (invalid)
        bs.write_bytes(b"\x00")  # payload
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_split_index_ge_count_rejected(self):
        """splitPacketIndex >= splitPacketCount should return None."""
        bs = BitStream()
        # RELIABLE = 2
        bs.write_bits(2, 3)
        bs.write_bit(True)  # has split
        bs.align_write_to_byte()
        bs.write_uint16(8)  # 1 byte of data
        bs.write_uint24(0)  # reliable_message_number
        bs.align_write_to_byte()
        # split info
        bs.write_uint32(3)  # split_count = 3
        bs.write_uint16(1)  # split_id
        bs.write_uint32(3)  # split_index = 3 (invalid, must be < 3)
        bs.write_bytes(b"\x00")  # payload
        bs.reset_read()
        assert decode_message_frame(bs) is None

    def test_valid_split_accepted(self):
        """splitPacketIndex < splitPacketCount should be accepted."""
        bs = BitStream()
        bs.write_bits(2, 3)
        bs.write_bit(True)
        bs.align_write_to_byte()
        bs.write_uint16(8)
        bs.write_uint24(0)
        bs.align_write_to_byte()
        bs.write_uint32(3)  # split_count = 3
        bs.write_uint16(1)  # split_id
        bs.write_uint32(2)  # split_index = 2 (valid)
        bs.write_bytes(b"\x00")
        bs.reset_read()
        decoded = decode_message_frame(bs)
        assert decoded is not None
        assert decoded.split_packet_index == 2
