"""Unit tests for BitStream bit/byte-level I/O."""

import pytest

from aiorak._bitstream import BitStream


class TestBitLevel:
    def test_write_read_bits(self):
        """Writing 3 bits (101) then 1 bit (1) reads back correctly."""
        bs = BitStream()
        bs.write_bits(5, 3)  # 101
        bs.write_bit(True)  # 1

        bs.reset_read()
        assert bs.read_bits(3) == 5
        assert bs.read_bit() is True

    def test_write_read_single_bits(self):
        """Writing individual bits (False, True, True, False) round-trips correctly."""
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
        """uint8 round-trips boundary values 0 and 255 correctly."""
        bs = BitStream()
        bs.write_uint8(0)
        bs.write_uint8(255)
        bs.reset_read()
        assert bs.read_uint8() == 0
        assert bs.read_uint8() == 255

    def test_write_read_uint16(self):
        """uint16 round-trips 0 and 0xABCD correctly."""
        bs = BitStream()
        bs.write_uint16(0)
        bs.write_uint16(0xABCD)
        bs.reset_read()
        assert bs.read_uint16() == 0
        assert bs.read_uint16() == 0xABCD

    def test_write_read_uint24(self):
        """uint24 round-trips 0, max (0xFFFFFF), and arbitrary value correctly."""
        bs = BitStream()
        bs.write_uint24(0)
        bs.write_uint24(0xFFFFFF)
        bs.write_uint24(0x123456)
        bs.reset_read()
        assert bs.read_uint24() == 0
        assert bs.read_uint24() == 0xFFFFFF
        assert bs.read_uint24() == 0x123456

    def test_write_read_uint32(self):
        """uint32 round-trips 0 and 0xDEADBEEF correctly."""
        bs = BitStream()
        bs.write_uint32(0)
        bs.write_uint32(0xDEADBEEF)
        bs.reset_read()
        assert bs.read_uint32() == 0
        assert bs.read_uint32() == 0xDEADBEEF

    def test_write_read_uint64(self):
        """uint64 round-trips 0 and 0xDEADBEEFCAFEBABE correctly."""
        bs = BitStream()
        bs.write_uint64(0)
        bs.write_uint64(0xDEADBEEFCAFEBABE)
        bs.reset_read()
        assert bs.read_uint64() == 0
        assert bs.read_uint64() == 0xDEADBEEFCAFEBABE

    def test_write_read_int64(self):
        """int64 round-trips -1, min, and max correctly."""
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
        """Aligning after 3 written bits pads the remaining 5 bits with zeros."""
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
        """Aligning when already byte-aligned does not change the write position."""
        bs = BitStream()
        bs.write_uint8(0xFF)
        pos_before = bs.write_bit_position
        bs.align_write_to_byte()
        assert bs.write_bit_position == pos_before


class TestAddress:
    def test_address_round_trip(self):
        """IPv4 address and port round-trip through write/read_address."""
        bs = BitStream()
        bs.write_address("127.0.0.1", 19132)
        bs.reset_read()
        host, port = bs.read_address()
        assert host == "127.0.0.1"
        assert port == 19132

    def test_address_inversion(self):
        """Written address octets are bitwise-inverted per RakNet wire format."""
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
        """Various IPv4 address/port combinations round-trip correctly."""
        for host, port in [("192.168.1.100", 0), ("0.0.0.0", 65535), ("10.20.30.40", 12345)]:
            bs = BitStream()
            bs.write_address(host, port)
            bs.reset_read()
            h, p = bs.read_address()
            assert h == host
            assert p == port


class TestPadding:
    def test_pad_with_zero(self):
        """pad_with_zero_to_byte_length extends buffer to target length with zeros."""
        bs = BitStream()
        bs.write_uint8(0xFF)
        bs.pad_with_zero_to_byte_length(10)
        assert bs.get_byte_length() == 10
        raw = bs.get_data()
        assert raw[0] == 0xFF
        assert raw[1:] == b"\x00" * 9


class TestCursorOps:
    def test_ignore_bits_bytes(self):
        """ignore_bytes and ignore_bits advance the read cursor correctly."""
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
        """Reading uint8 past end of buffer raises ValueError."""
        bs = BitStream()
        bs.write_uint8(1)
        bs.reset_read()
        bs.read_uint8()
        with pytest.raises(ValueError):
            bs.read_uint8()

    def test_read_bits_past_end_raises(self):
        """Reading more bits than available raises ValueError."""
        bs = BitStream()
        bs.write_bit(True)
        bs.reset_read()
        with pytest.raises(ValueError):
            bs.read_bits(2)


class TestEmptyStream:
    def test_empty_stream_properties(self):
        """A fresh BitStream has zero positions, zero length, and empty data."""
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
        """Raw bytes round-trip through write_bytes/read_bytes."""
        bs = BitStream()
        payload = b"\x01\x02\x03\x04\x05"
        bs.write_bytes(payload)
        bs.reset_read()
        assert bs.read_bytes(5) == payload

    def test_init_from_data(self):
        """BitStream initialized from data can be read immediately."""
        data = b"\xde\xad\xbe\xef"
        bs = BitStream(data)
        assert bs.read_uint32() == 0xDEADBEEF  # big-endian


class TestUnalignedBytes:
    def test_unaligned_write_read(self):
        """Writing bytes at a non-byte boundary reads back correctly via unaligned methods."""
        bs = BitStream()
        bs.write_bits(0b101, 3)  # 3 bits offset
        bs.write_bytes_unaligned(b"\xab\xcd")
        bs.reset_read()
        assert bs.read_bits(3) == 0b101
        assert bs.read_bytes_unaligned(2) == b"\xab\xcd"

    def test_aligned_fast_path(self):
        """When already aligned, unaligned methods use the fast memcpy path."""
        bs = BitStream()
        payload = b"\x01\x02\x03\x04\x05"
        bs.write_bytes_unaligned(payload)
        bs.reset_read()
        assert bs.read_bytes_unaligned(5) == payload

    def test_read_past_end_raises(self):
        """Reading more unaligned bytes than available raises ValueError."""
        bs = BitStream()
        bs.write_uint8(0xFF)
        bs.reset_read()
        with pytest.raises(ValueError):
            bs.read_bytes_unaligned(2)


class TestBitStreamErrors:
    """Error paths and edge cases for BitStream methods."""

    def test_ignore_bits_overflow(self):
        """Ignoring bits on an empty stream raises ValueError."""
        bs = BitStream()
        with pytest.raises(ValueError, match="Not enough bits"):
            bs.ignore_bits(1)

    def test_read_bit_past_end(self):
        """Reading a single bit from an empty stream raises ValueError."""
        bs = BitStream()
        with pytest.raises(ValueError, match="Read past end"):
            bs.read_bit()

    def test_read_uint16_short(self):
        """Reading uint16 from a 1-byte buffer raises ValueError.

        The buffer contains only 8 bits but uint16 requires 16.
        Verifies the bounds check before the struct.unpack call.
        """
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint16"):
            bs.read_uint16()

    def test_read_uint24_short(self):
        """Reading uint24 from a 1-byte buffer raises ValueError."""
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint24"):
            bs.read_uint24()

    def test_read_uint32_short(self):
        """Reading uint32 from a 1-byte buffer raises ValueError."""
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint32"):
            bs.read_uint32()

    def test_read_uint64_short(self):
        """Reading uint64 from a 1-byte buffer raises ValueError."""
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint64"):
            bs.read_uint64()

    def test_read_int64_short(self):
        """Reading int64 from a 1-byte buffer raises ValueError."""
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="int64"):
            bs.read_int64()

    def test_read_bytes_overflow(self):
        """Reading more bytes than available raises ValueError."""
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="Cannot read 5 bytes"):
            bs.read_bytes(5)

    def test_read_address_ipv4_truncated(self):
        """Reading IPv4 address from truncated buffer raises ValueError."""
        bs = BitStream()
        bs.write_uint8(4)  # af=4
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="IPv4"):
            bs2.read_address()

    def test_read_address_ipv6_truncated(self):
        """Reading IPv6 address from truncated buffer raises ValueError."""
        bs = BitStream()
        bs.write_uint8(6)  # af=6
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="IPv6"):
            bs2.read_address()

    def test_read_address_bad_family(self):
        """Reading address with unsupported family byte raises ValueError."""
        bs = BitStream()
        bs.write_uint8(99)
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="Unsupported address family: 99"):
            bs2.read_address()

    def test_pad_already_sufficient(self):
        """Padding to a length smaller than current size is a no-op."""
        bs = BitStream(b"\x00" * 10)
        bs.pad_with_zero_to_byte_length(5)
        assert bs.get_byte_length() == 10

    def test_repr(self):
        """BitStream repr includes class name and cursor positions."""
        bs = BitStream()
        r = repr(bs)
        assert "BitStream" in r
        assert "write_bits=0" in r
        assert "read_bits=0" in r
