import socket
import struct
from typing import Literal, Union

# Define a reusable endianness literal type
Endian = Literal["big", "little"]


class ByteStream:
    """
    A binary stream with separate read and write pointers.
    - Initialize with None, bytes/bytearray (copied), or memoryview (zero-copy until write).
    - `read_offset` and `write_offset` allow independent control of read/write positions.
    - `data` provides a view of currently unread bytes.
    - `write` and `read` for raw bytes; convenience methods for integers with configurable endianness.
    - `skip_bytes` to advance the read pointer without reading.
    """

    def __init__(self, data: Union[bytes, bytearray, memoryview, None] = None) -> None:
        self._read_pos = 0
        # Initialize buffer
        if data is None:
            self._buffer: Union[bytearray, memoryview] = bytearray()
            self._using_view = False
        elif isinstance(data, memoryview):
            self._buffer = data
            self._using_view = True
        elif isinstance(data, (bytes, bytearray)):
            self._buffer = bytearray(data)
            self._using_view = False
        else:
            raise TypeError(f"Unsupported data type: {type(data)}")

    @property
    def read_offset(self) -> int:
        return self._read_pos

    @read_offset.setter
    def read_offset(self, pos: int) -> None:
        if pos < 0:
            raise ValueError("Position must be non-negative")
        self._read_pos = pos

    @property
    def data(self) -> memoryview:
        buf = self._buffer
        if isinstance(buf, memoryview):
            return buf[self._read_pos:]
        return memoryview(buf)[self._read_pos:]

    def write(self, data: Union[bytes, bytearray, memoryview]) -> None:
        if self._using_view:
            self._buffer = bytearray(self._buffer)
            self._using_view = False

        self._buffer.extend(data)

    def write_byte(self, value: int) -> None:
        self.write(struct.pack("B", value & 0xFF))

    def write_bool(self, value: bool) -> None:
        self.write_byte(1 if value else 0)

    def write_short(self, value: int, endian: Endian = "big") -> None:
        fmt = ">H" if endian == "big" else "<H"
        self.write(struct.pack(fmt, value & 0xFFFF))

    def write_medium(self, value: int, endian: Endian = "big") -> None:
        v = value & 0xFFFFFF
        bytes_be = [(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]
        b = bytes_be if endian == "big" else bytes_be[::-1]
        self.write(bytes(b))

    def write_int(self, value: int, endian: Endian = "big") -> None:
        fmt = ">I" if endian == "big" else "<I"
        self.write(struct.pack(fmt, value & 0xFFFFFFFF))

    def write_long(self, value: int, endian: Endian = "big") -> None:
        fmt = ">Q" if endian == "big" else "<Q"
        self.write(struct.pack(fmt, value & 0xFFFFFFFFFFFFFFFF))

    def write_address(self, addr: tuple[str, int]) -> None:
        try:
            packed = socket.inet_aton(addr[0])
            packed = bytes((~b & 0xFF) for b in packed)
            self.write_byte(4)
            self.write(packed)
            self.write_short(addr[1])

        except socket.error:
            packed = socket.inet_pton(socket.AF_INET6, addr[0])
            self.write_byte(6)
            self.write_short(socket.AF_INET6)
            self.write_short(addr[1], endian="little")
            self.write_int(0)  # sin6_flowinfo
            self.write(packed)  # sin6_addr
            self.write_int(0)  # sin6_scope_id

    def read(self, size: int) -> memoryview:
        """
        Read exactly size bytes from read_offset, return as memoryview, advance read_offset.
        Raises ValueError if size <= 0.
        """
        if size <= 0:
            raise ValueError("size must be a positive integer")
        buf = memoryview(self._buffer)[self._read_pos:]
        mv = buf[:size]
        self._read_pos += size
        return mv

    def read_byte(self) -> int:
        return self.read(1)[0]

    def read_bool(self) -> bool:
        return bool(self.read_byte())

    def read_short(self, endian: Endian = "big") -> int:
        mv = self.read(2)
        fmt = ">H" if endian == "big" else "<H"
        return struct.unpack(fmt, mv)[0]

    def read_medium(self, endian: Endian = "big") -> int:
        mv = self.read(3)
        if endian == "big":
            return (mv[0] << 16) | (mv[1] << 8) | mv[2]
        return (mv[2] << 16) | (mv[1] << 8) | mv[0]

    def read_int(self, endian: Endian = "big") -> int:
        mv = self.read(4)
        fmt = ">I" if endian == "big" else "<I"
        return struct.unpack(fmt, mv)[0]

    def read_float(self, endian: Endian = "big") -> int:
        mv = self.read(4)
        fmt = ">f" if endian == "big" else "<f"
        return struct.unpack(fmt, mv)[0]

    def read_long(self, endian: Endian = "big") -> int:
        mv = self.read(8)
        fmt = ">Q" if endian == "big" else "<Q"
        return struct.unpack(fmt, mv)[0]

    def read_address(self) -> tuple[str, int]:
        addr_type = self.read_byte()
        if addr_type == 4:  # IPv4
            packed = self.read(4)
            unhidden = bytes((~b & 0xFF) for b in packed)
            ip = socket.inet_ntoa(unhidden)
            port = self.read_short()
            return ip, port

        elif addr_type == 6:  # IPv6
            self.skip_bytes(2)  # sin6_family
            port = self.read_short()
            self.skip_bytes(4)  # sin6_flowinfo
            packed = self.read(16)
            ip = socket.inet_ntop(socket.AF_INET6, packed)
            self.skip_bytes(4)  # sin6_scope_id
            return ip, port
        else:
            raise ValueError("Invalid address type")

    def skip_bytes(self, n: int) -> int:
        """
        Advance the read_offset by n bytes without reading.
        Raises ValueError if n <= 0.
        Returns the new read_offset.
        """
        if n <= 0:
            raise ValueError("n must be a positive integer")
        new_pos = min(self._read_pos + n, len(self._buffer))
        self._read_pos = new_pos
        return self._read_pos


# Example usage
if __name__ == "__main__":
    bs = ByteStream(b"\x01\x02\x03\x04\x05")
    print(bs.read_byte(), bs.skip_bytes(2), bs.read_byte())  # 1, 3, 4
