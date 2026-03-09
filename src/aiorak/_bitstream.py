"""Bit-level read/write serialization compatible with the RakNet BitStream.

The RakNet wire protocol encodes reliability metadata at the *bit* level
(e.g. a 3-bit reliability field followed by a 1-bit split flag), so a
simple byte-oriented buffer is insufficient.  This module provides
:class:`BitStream` with both bit-granular and byte-aligned accessors that
mirror the C++ ``RakNet::BitStream`` API closely enough to produce
wire-compatible output.

Byte order
----------
Multi-byte integers in RakNet datagrams are **big-endian** (the C++ BitStream
endian-swaps on little-endian hosts).  The one exception is ``uint24_t``, which
has a specialization that writes in native (little-endian) order.

Usage::

    bs = BitStream()
    bs.write_uint8(0x05)
    bs.write_bits(3, 3)        # 3-bit value
    bs.write_bit(True)         # 1-bit flag
    bs.align_write_to_byte()
    bs.write_uint16(1024)      # big-endian u16
    raw = bs.get_data()
"""

import struct


class BitStream:
    """Variable-length bit-level I/O buffer.

    Internally the data is stored in a :class:`bytearray`.  A *write cursor*
    (``_write_bit_pos``) and a *read cursor* (``_read_bit_pos``) track the
    current position independently so that the same stream can be written and
    then rewound for reading.

    Args:
        data: Optional initial bytes to wrap for reading.
    """

    __slots__ = ("_buf", "_write_bit_pos", "_read_bit_pos")

    def __init__(self, data: bytes | bytearray | None = None) -> None:
        if data is not None:
            self._buf = bytearray(data)
            self._write_bit_pos = len(self._buf) * 8
        else:
            self._buf = bytearray()
            self._write_bit_pos = 0
        self._read_bit_pos = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def write_bit_position(self) -> int:
        """Current write cursor position in bits."""
        return self._write_bit_pos

    @property
    def read_bit_position(self) -> int:
        """Current read cursor position in bits."""
        return self._read_bit_pos

    @property
    def unread_bits(self) -> int:
        """Number of bits remaining between the read cursor and the write cursor."""
        return max(0, self._write_bit_pos - self._read_bit_pos)

    @property
    def unread_bytes(self) -> int:
        """Number of *whole* bytes remaining to read."""
        return self.unread_bits // 8

    # ------------------------------------------------------------------
    # Raw data access
    # ------------------------------------------------------------------

    def get_data(self) -> bytes:
        """Return the written portion of the buffer as immutable bytes."""
        byte_len = (self._write_bit_pos + 7) // 8
        return bytes(self._buf[:byte_len])

    def get_byte_length(self) -> int:
        """Number of bytes required to hold all written bits."""
        return (self._write_bit_pos + 7) // 8

    def reset_read(self) -> None:
        """Rewind the read cursor to the beginning."""
        self._read_bit_pos = 0

    def ignore_bits(self, num_bits: int) -> None:
        """Advance the read cursor by *num_bits* without returning data.

        Raises:
            ValueError: If there are not enough bits remaining.
        """
        if self._read_bit_pos + num_bits > self._write_bit_pos:
            raise ValueError("Not enough bits to ignore")
        self._read_bit_pos += num_bits

    def ignore_bytes(self, num_bytes: int) -> None:
        """Advance the read cursor by *num_bytes* whole bytes.

        Raises:
            ValueError: If there are not enough bits remaining.
        """
        self.ignore_bits(num_bytes * 8)

    # ------------------------------------------------------------------
    # Bit-level write
    # ------------------------------------------------------------------

    def _ensure_capacity(self, extra_bits: int) -> None:
        """Grow the underlying buffer if necessary to hold *extra_bits* more."""
        needed_bytes = (self._write_bit_pos + extra_bits + 7) // 8
        if needed_bytes > len(self._buf):
            self._buf.extend(b"\x00" * (needed_bytes - len(self._buf)))

    def write_bit(self, value: bool) -> None:
        """Write a single bit.

        Args:
            value: ``True`` writes a ``1`` bit, ``False`` writes a ``0`` bit.
        """
        self._ensure_capacity(1)
        byte_idx = self._write_bit_pos >> 3
        bit_idx = 7 - (self._write_bit_pos & 7)  # MSB-first within each byte
        if value:
            self._buf[byte_idx] |= 1 << bit_idx
        else:
            self._buf[byte_idx] &= ~(1 << bit_idx)
        self._write_bit_pos += 1

    def write_bits(self, value: int, num_bits: int) -> None:
        """Write the lowest *num_bits* of *value*, MSB first.

        This matches the C++ ``BitStream::WriteBits`` behaviour where the
        most-significant of the requested bits is written first.

        Args:
            value: Integer whose lowest *num_bits* bits will be written.
            num_bits: How many bits to write (1–64).
        """
        for i in range(num_bits - 1, -1, -1):
            self.write_bit(bool((value >> i) & 1))

    # ------------------------------------------------------------------
    # Bit-level read
    # ------------------------------------------------------------------

    def read_bit(self) -> bool:
        """Read a single bit and advance the read cursor.

        Returns:
            ``True`` if the bit is 1, ``False`` otherwise.

        Raises:
            ValueError: If there are no more bits to read.
        """
        if self._read_bit_pos >= self._write_bit_pos:
            raise ValueError("Read past end of BitStream")
        byte_idx = self._read_bit_pos >> 3
        bit_idx = 7 - (self._read_bit_pos & 7)
        self._read_bit_pos += 1
        return bool((self._buf[byte_idx] >> bit_idx) & 1)

    def read_bits(self, num_bits: int) -> int:
        """Read *num_bits* and return them as an integer (MSB first).

        Args:
            num_bits: How many bits to read (1–64).

        Returns:
            An unsigned integer assembled from the read bits.

        Raises:
            ValueError: If there are not enough bits remaining.
        """
        if self._read_bit_pos + num_bits > self._write_bit_pos:
            raise ValueError(f"Cannot read {num_bits} bits, only {self.unread_bits} available")
        result = 0
        for _ in range(num_bits):
            result = (result << 1) | int(self.read_bit())
        return result

    # ------------------------------------------------------------------
    # Alignment
    # ------------------------------------------------------------------

    def align_write_to_byte(self) -> None:
        """Advance the write cursor to the next byte boundary (zero-fill).

        If the cursor is already byte-aligned this is a no-op.
        """
        remainder = self._write_bit_pos & 7
        if remainder:
            pad = 8 - remainder
            self._ensure_capacity(pad)
            # Bits are already zero from _ensure_capacity / initial bytearray
            self._write_bit_pos += pad

    def align_read_to_byte(self) -> None:
        """Advance the read cursor to the next byte boundary.

        If the cursor is already byte-aligned this is a no-op.
        """
        remainder = self._read_bit_pos & 7
        if remainder:
            self._read_bit_pos += 8 - remainder

    # ------------------------------------------------------------------
    # Byte-aligned integer writes — big-endian unless noted
    # ------------------------------------------------------------------

    def write_uint8(self, value: int) -> None:
        """Write an 8-bit unsigned integer."""
        self.align_write_to_byte()
        self._ensure_capacity(8)
        self._buf[self._write_bit_pos >> 3] = value & 0xFF
        self._write_bit_pos += 8

    def write_uint16(self, value: int) -> None:
        """Write a 16-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_write_to_byte()
        self._ensure_capacity(16)
        idx = self._write_bit_pos >> 3
        self._buf[idx : idx + 2] = struct.pack(">H", value & 0xFFFF)
        self._write_bit_pos += 16

    def write_uint24(self, value: int) -> None:
        """Write a 24-bit unsigned integer (little-endian).

        Used for datagram and reliable message sequence numbers.
        """
        self.align_write_to_byte()
        self._ensure_capacity(24)
        idx = self._write_bit_pos >> 3
        self._buf[idx] = value & 0xFF
        self._buf[idx + 1] = (value >> 8) & 0xFF
        self._buf[idx + 2] = (value >> 16) & 0xFF
        self._write_bit_pos += 24

    def write_uint32(self, value: int) -> None:
        """Write a 32-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_write_to_byte()
        self._ensure_capacity(32)
        idx = self._write_bit_pos >> 3
        self._buf[idx : idx + 4] = struct.pack(">I", value & 0xFFFFFFFF)
        self._write_bit_pos += 32

    def write_uint64(self, value: int) -> None:
        """Write a 64-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_write_to_byte()
        self._ensure_capacity(64)
        idx = self._write_bit_pos >> 3
        self._buf[idx : idx + 8] = struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF)
        self._write_bit_pos += 64

    def write_int64(self, value: int) -> None:
        """Write a 64-bit signed integer (big-endian, matching C++ BitStream)."""
        self.align_write_to_byte()
        self._ensure_capacity(64)
        idx = self._write_bit_pos >> 3
        self._buf[idx : idx + 8] = struct.pack(">q", value)
        self._write_bit_pos += 64

    # ------------------------------------------------------------------
    # Byte-aligned integer reads — big-endian unless noted
    # ------------------------------------------------------------------

    def read_uint8(self) -> int:
        """Read an 8-bit unsigned integer."""
        self.align_read_to_byte()
        if self._read_bit_pos + 8 > self._write_bit_pos:
            raise ValueError("Not enough data to read uint8")
        val = self._buf[self._read_bit_pos >> 3]
        self._read_bit_pos += 8
        return val

    def read_uint16(self) -> int:
        """Read a 16-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_read_to_byte()
        if self._read_bit_pos + 16 > self._write_bit_pos:
            raise ValueError("Not enough data to read uint16")
        idx = self._read_bit_pos >> 3
        (val,) = struct.unpack_from(">H", self._buf, idx)
        self._read_bit_pos += 16
        return val

    def read_uint24(self) -> int:
        """Read a 24-bit unsigned integer (little-endian)."""
        self.align_read_to_byte()
        if self._read_bit_pos + 24 > self._write_bit_pos:
            raise ValueError("Not enough data to read uint24")
        idx = self._read_bit_pos >> 3
        val = self._buf[idx] | (self._buf[idx + 1] << 8) | (self._buf[idx + 2] << 16)
        self._read_bit_pos += 24
        return val

    def read_uint32(self) -> int:
        """Read a 32-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_read_to_byte()
        if self._read_bit_pos + 32 > self._write_bit_pos:
            raise ValueError("Not enough data to read uint32")
        idx = self._read_bit_pos >> 3
        (val,) = struct.unpack_from(">I", self._buf, idx)
        self._read_bit_pos += 32
        return val

    def read_uint64(self) -> int:
        """Read a 64-bit unsigned integer (big-endian, matching C++ BitStream)."""
        self.align_read_to_byte()
        if self._read_bit_pos + 64 > self._write_bit_pos:
            raise ValueError("Not enough data to read uint64")
        idx = self._read_bit_pos >> 3
        (val,) = struct.unpack_from(">Q", self._buf, idx)
        self._read_bit_pos += 64
        return val

    def read_int64(self) -> int:
        """Read a 64-bit signed integer (big-endian, matching C++ BitStream)."""
        self.align_read_to_byte()
        if self._read_bit_pos + 64 > self._write_bit_pos:
            raise ValueError("Not enough data to read int64")
        idx = self._read_bit_pos >> 3
        (val,) = struct.unpack_from(">q", self._buf, idx)
        self._read_bit_pos += 64
        return val

    # ------------------------------------------------------------------
    # Raw byte I/O
    # ------------------------------------------------------------------

    def write_bytes(self, data: bytes | bytearray) -> None:
        """Write raw bytes at the current (byte-aligned) position.

        The write cursor is aligned to a byte boundary before writing.

        Args:
            data: The bytes to append.
        """
        self.align_write_to_byte()
        n = len(data) * 8
        self._ensure_capacity(n)
        idx = self._write_bit_pos >> 3
        self._buf[idx : idx + len(data)] = data
        self._write_bit_pos += n

    def read_bytes(self, num_bytes: int) -> bytes:
        """Read *num_bytes* raw bytes from the current (byte-aligned) position.

        Args:
            num_bytes: Number of bytes to read.

        Returns:
            The read bytes.

        Raises:
            ValueError: If there are not enough bytes remaining.
        """
        self.align_read_to_byte()
        n = num_bytes * 8
        if self._read_bit_pos + n > self._write_bit_pos:
            raise ValueError(f"Cannot read {num_bytes} bytes, only {self.unread_bytes} available")
        idx = self._read_bit_pos >> 3
        result = bytes(self._buf[idx : idx + num_bytes])
        self._read_bit_pos += n
        return result

    # ------------------------------------------------------------------
    # Address encoding — RakNet SystemAddress format
    # ------------------------------------------------------------------

    def write_address(self, host: str, port: int) -> None:
        """Write an IPv4 address in RakNet ``SystemAddress`` wire format.

        Format: ``address_family(1) + inverted_ip(4) + port_be(2)`` = 7 bytes.
        RakNet uses AF_INET = 4 and inverts each IP octet with bitwise NOT.

        Args:
            host: Dotted-quad IPv4 address string (e.g. ``"127.0.0.1"``).
            port: UDP port number (0–65535).
        """
        self.align_write_to_byte()
        octets = [int(o) for o in host.split(".")]
        self._ensure_capacity(7 * 8)
        idx = self._write_bit_pos >> 3
        # Address family (IPv4 = 4)
        self._buf[idx] = 4
        # Inverted IP octets
        for i, octet in enumerate(octets):
            self._buf[idx + 1 + i] = (~octet) & 0xFF
        # Port in big-endian
        self._buf[idx + 5 : idx + 7] = struct.pack(">H", port & 0xFFFF)
        self._write_bit_pos += 7 * 8

    def read_address(self) -> tuple[str, int]:
        """Read an IPv4 address from RakNet ``SystemAddress`` wire format.

        Returns:
            ``(host, port)`` tuple where *host* is a dotted-quad string.

        Raises:
            ValueError: If the address family is not IPv4 or data is missing.
        """
        self.align_read_to_byte()
        if self._read_bit_pos + 7 * 8 > self._write_bit_pos:
            raise ValueError("Not enough data to read address")
        idx = self._read_bit_pos >> 3
        af = self._buf[idx]
        if af != 4:
            raise ValueError(f"Unsupported address family: {af} (expected 4/IPv4)")
        octets = [str((~self._buf[idx + 1 + i]) & 0xFF) for i in range(4)]
        host = ".".join(octets)
        (port,) = struct.unpack_from(">H", self._buf, idx + 5)
        self._read_bit_pos += 7 * 8
        return host, port

    # ------------------------------------------------------------------
    # Padding
    # ------------------------------------------------------------------

    def pad_with_zero_to_byte_length(self, target_length: int) -> None:
        """Extend the buffer with zero bytes until it reaches *target_length* bytes.

        Used during MTU discovery to pad ``ID_OPEN_CONNECTION_REQUEST_1`` to
        the desired MTU size.

        Args:
            target_length: Desired total byte length of the buffer.
        """
        current_len = self.get_byte_length()
        if current_len < target_length:
            self.align_write_to_byte()
            pad = target_length - current_len
            self._ensure_capacity(pad * 8)
            # Bytes are already zeroed by _ensure_capacity
            self._write_bit_pos += pad * 8

    def __len__(self) -> int:
        """Return the number of bytes required to hold all written bits."""
        return self.get_byte_length()

    def __repr__(self) -> str:
        return f"BitStream(write_bits={self._write_bit_pos}, read_bits={self._read_bit_pos}, buf_len={len(self._buf)})"
