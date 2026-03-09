"""Unit tests for BitStream bit/byte-level I/O."""

from __future__ import annotations

import pytest

from aiorak._bitstream import BitStream


class TestBitLevel:
    def test_write_read_bits(self):
        bs = BitStream()
        bs.write_bits(5, 3)  # 101
        bs.write_bit(True)  # 1

        bs.reset_read()
        assert bs.read_bits(3) == 5
        assert bs.read_bit() is True

    def test_write_read_single_bits(self):
        bs = BitStream()
        bs.write_bit(False)
        bs.write_bit(True)
        bs.write_bit(True)
        bs.write_bit(False)

        bs.reset_read()
        assert bs.read_bit() is False
        assert bs.read_bit() is True
        assert bs.read_bit() is True
        assert bs.read_bit() is False


class TestIntegerRoundTrips:
    def test_write_read_uint8(self):
        bs = BitStream()
        bs.write_uint8(0)
        bs.write_uint8(255)
        bs.reset_read()
        assert bs.read_uint8() == 0
        assert bs.read_uint8() == 255

    def test_write_read_uint16(self):
        bs = BitStream()
        bs.write_uint16(0)
        bs.write_uint16(0xABCD)
        bs.reset_read()
        assert bs.read_uint16() == 0
        assert bs.read_uint16() == 0xABCD

    def test_write_read_uint24(self):
        bs = BitStream()
        bs.write_uint24(0)
        bs.write_uint24(0xFFFFFF)
        bs.write_uint24(0x123456)
        bs.reset_read()
        assert bs.read_uint24() == 0
        assert bs.read_uint24() == 0xFFFFFF
        assert bs.read_uint24() == 0x123456

    def test_write_read_uint32(self):
        bs = BitStream()
        bs.write_uint32(0)
        bs.write_uint32(0xDEADBEEF)
        bs.reset_read()
        assert bs.read_uint32() == 0
        assert bs.read_uint32() == 0xDEADBEEF

    def test_write_read_uint64(self):
        bs = BitStream()
        bs.write_uint64(0)
        bs.write_uint64(0xDEADBEEFCAFEBABE)
        bs.reset_read()
        assert bs.read_uint64() == 0
        assert bs.read_uint64() == 0xDEADBEEFCAFEBABE

    def test_write_read_int64(self):
        bs = BitStream()
        bs.write_int64(-1)
        bs.write_int64(-(2**63))
        bs.write_int64(2**63 - 1)
        bs.reset_read()
        assert bs.read_int64() == -1
        assert bs.read_int64() == -(2**63)
        assert bs.read_int64() == 2**63 - 1


class TestAlignment:
    def test_alignment_pads_with_zeros(self):
        bs = BitStream()
        bs.write_bits(0b101, 3)
        bs.align_write_to_byte()
        bs.write_uint8(0x42)

        bs.reset_read()
        val = bs.read_bits(3)
        assert val == 0b101
        # Remaining 5 bits of first byte should be 0
        pad = bs.read_bits(5)
        assert pad == 0
        assert bs.read_uint8() == 0x42

    def test_align_noop_when_aligned(self):
        bs = BitStream()
        bs.write_uint8(0xFF)
        pos_before = bs.write_bit_position
        bs.align_write_to_byte()
        assert bs.write_bit_position == pos_before


class TestAddress:
    def test_address_round_trip(self):
        bs = BitStream()
        bs.write_address("127.0.0.1", 19132)
        bs.reset_read()
        host, port = bs.read_address()
        assert host == "127.0.0.1"
        assert port == 19132

    def test_address_inversion(self):
        bs = BitStream()
        bs.write_address("127.0.0.1", 19132)
        raw = bs.get_data()
        # AF byte at index 0, then 4 inverted octets
        assert raw[0] == 4  # AF_INET
        assert raw[1] == (~127) & 0xFF
        assert raw[2] == (~0) & 0xFF
        assert raw[3] == (~0) & 0xFF
        assert raw[4] == (~1) & 0xFF

    def test_address_various(self):
        for host, port in [("192.168.1.100", 0), ("0.0.0.0", 65535), ("10.20.30.40", 12345)]:
            bs = BitStream()
            bs.write_address(host, port)
            bs.reset_read()
            h, p = bs.read_address()
            assert h == host
            assert p == port


class TestPadding:
    def test_pad_with_zero(self):
        bs = BitStream()
        bs.write_uint8(0xFF)
        bs.pad_with_zero_to_byte_length(10)
        assert bs.get_byte_length() == 10
        raw = bs.get_data()
        assert raw[0] == 0xFF
        assert raw[1:] == b"\x00" * 9


class TestCursorOps:
    def test_ignore_bits_bytes(self):
        bs = BitStream()
        bs.write_uint8(0xAA)
        bs.write_uint8(0xBB)
        bs.write_uint8(0xCC)
        bs.reset_read()
        bs.ignore_bytes(1)
        assert bs.read_uint8() == 0xBB
        bs.ignore_bits(8)
        assert bs.unread_bits == 0

    def test_read_past_end_raises(self):
        bs = BitStream()
        bs.write_uint8(1)
        bs.reset_read()
        bs.read_uint8()
        with pytest.raises(ValueError):
            bs.read_uint8()

    def test_read_bits_past_end_raises(self):
        bs = BitStream()
        bs.write_bit(True)
        bs.reset_read()
        with pytest.raises(ValueError):
            bs.read_bits(2)


class TestEmptyStream:
    def test_empty_stream_properties(self):
        bs = BitStream()
        assert bs.write_bit_position == 0
        assert bs.read_bit_position == 0
        assert bs.unread_bits == 0
        assert bs.unread_bytes == 0
        assert bs.get_data() == b""
        assert bs.get_byte_length() == 0
        assert len(bs) == 0


class TestRawBytes:
    def test_write_read_bytes(self):
        bs = BitStream()
        payload = b"\x01\x02\x03\x04\x05"
        bs.write_bytes(payload)
        bs.reset_read()
        assert bs.read_bytes(5) == payload

    def test_init_from_data(self):
        data = b"\xDE\xAD\xBE\xEF"
        bs = BitStream(data)
        assert bs.read_uint32() == 0xEFBEADDE  # little-endian
