import asyncio
import enum
import typing
from collections import deque
from dataclasses import dataclass

from . import constants
from .constants import NUMBER_OF_ORDERED_STREAMS
from .sliding_window import SlidingWindow
from .stream import ByteStream


class Reliability(enum.IntEnum):
    UNRELIABLE = 0
    UNRELIABLE_SEQUENCED = 1
    RELIABLE = 2
    RELIABLE_ORDERED = 3
    RELIABLE_SEQUENCED = 4

    @property
    def reliable(self) -> bool:
        return self in {
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
        }

    @property
    def sequenced(self) -> bool:
        return self in {Reliability.UNRELIABLE_SEQUENCED, Reliability.RELIABLE_SEQUENCED}

    @property
    def ordered(self) -> bool:
        return self is Reliability.RELIABLE_ORDERED or self.sequenced

    @classmethod
    def from_flags(cls, reliable: bool, ordered: bool, sequenced: bool) -> "Reliability":
        match reliable, ordered, sequenced:
            case False, False, True:
                return cls.UNRELIABLE_SEQUENCED
            case False, False, False:
                return cls.UNRELIABLE
            case True, True, False:
                return cls.RELIABLE_ORDERED
            case True, False, True:
                return cls.RELIABLE_SEQUENCED
            case True, False, False:
                return cls.RELIABLE
            case _:
                raise ValueError("Invalid combination of flags")


@dataclass(slots=True)
class Message:
    MAX_HEADER_SIZE: typing.ClassVar[int] = 23  # worse case scenario
    data: bytes
    reliability: Reliability
    reliable_id: int | None = None
    ordering_id: int | None = None
    sequencing_id: int | None = None
    channel: int | None = None
    split_id: int | None = None
    split_index: int | None = None
    split_count: int | None = None
    resend_handle: asyncio.TimerHandle | None = None

    @property
    def header_size(self) -> int:
        length = 1  # flag
        length += 2  # bit size
        if self.reliability.sequenced:
            length += 3  # reliable id
        if self.reliability.sequenced:
            length += 3  # sequencing id
        if self.reliability.ordered:
            length += 3 + 1  # ordering id + channel
        if self.split_id is not None:
            length += 4 + 2 + 4  # split id + index + count
        return length

    def write(self, stream: ByteStream) -> None:
        flag = self.reliability << 5
        if self.split_id is not None:
            flag |= 1 << 4

        stream.write_byte(flag)
        stream.write_short(len(self.data) * 8)
        if self.reliability.reliable:
            stream.write_medium(self.reliable_id, endian="little")
        if self.reliability.sequenced:
            stream.write_medium(self.sequencing_id, endian="little")
        if self.reliability.ordered:
            stream.write_medium(self.ordering_id, endian="little")
            stream.write_byte(self.channel)
        if self.split_id is not None:
            stream.write_int(self.split_count)
            stream.write_short(self.split_id)
            stream.write_int(self.split_index)
        stream.write(self.data)

    @classmethod
    def from_stream(cls, stream: ByteStream) -> "Message":
        flag = stream.read_byte()
        reliability = Reliability(flag >> 5)
        split = flag & (1 << 4) != 0
        bit_size = stream.read_short()
        message = Message(data=b"", reliability=reliability)
        if reliability.reliable:
            message.reliable_id = stream.read_medium(endian="little")
        if reliability.sequenced:
            message.sequencing_id = stream.read_medium(endian="little")
        if reliability.sequenced or reliability.ordered:
            message.ordering_id = stream.read_medium(endian="little")
            message.channel = stream.read_byte()
        if split:
            message.split_count = stream.read_int()
            message.split_id = stream.read_short()
            message.split_index = stream.read_int()

        message.data = bytes(stream.read(bit_size // 8))
        return message


@dataclass(slots=True)
class DatagramHeader:
    SIZE: typing.ClassVar[int] = 4  # flag (1 byte) + datagram_id (3 bytes)
    is_ack: bool = False
    is_nak: bool = False
    is_continuous_send: bool = False
    is_in_slow_start: bool = False
    id: int | None = None

    def write(self, stream: ByteStream) -> None:
        flag = 1 << 7  # valid
        if self.is_ack:
            flag |= 1 << 6
            stream.write_byte(flag)
        elif self.is_nak:
            flag |= 1 << 5
            stream.write_byte(flag)
        else:
            flag |= (1 << 3) if self.is_continuous_send else 0
            flag |= (1 << 2) if self.is_in_slow_start else 0
            stream.write_byte(flag)
            stream.write_medium(self.id, endian="little")

    @classmethod
    def from_stream(cls, stream: ByteStream) -> "DatagramHeader":
        flag = stream.read_byte()
        assert bool(flag & (1 << 7)), "Invalid datagram header"
        if bool(flag & (1 << 6)):
            return cls(is_ack=True)

        if bool(flag & (1 << 5)):
            return cls(is_nak=True)

        is_continuous_send = bool(flag & (1 << 3))
        is_in_slow_start = bool(flag & (1 << 2))
        id = stream.read_medium(endian="little")
        return cls(is_continuous_send=is_continuous_send, is_in_slow_start=is_in_slow_start, id=id)


class ReliabilityLayer:
    def __init__(self, transport: asyncio.DatagramTransport, remote_addr: tuple[str, int], mtu: int):
        assert mtu <= constants.MAXIMUM_MTU_SIZE, "MTU is too large"
        self.loop = asyncio.get_event_loop()
        self.transport = transport
        self._remote_addr = remote_addr
        self._cc = SlidingWindow(mtu - constants.UDP_HEADER_SIZE)
        self._send_queue: deque[Message] = deque()
        self._resend_queue: deque[Message] = deque()
        self._reliable_id = 0
        self._ordered_ids = [0] * NUMBER_OF_ORDERED_STREAMS
        self._sequenced_ids = [0] * NUMBER_OF_ORDERED_STREAMS
        self._flush_handle: asyncio.TimerHandle | None = None
        self._unacked_bytes: int = 0
        self._datagram_history: dict[int, list[tuple[Message, float]]] = {}
        self._bandwidth_exceeded = False
        self._acks: set[int] = set()
        self._naks: set[int] = set()
        self.send_acks_handle: asyncio.TimerHandle | None = None
        self.send_naks_handle: asyncio.TimerHandle | None = None

    def send(
        self,
        data: bytes | memoryview,
        *,
        reliable: bool,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
        immediate: bool = False,
    ) -> None:
        assert 0 <= channel < constants.NUMBER_OF_ORDERED_STREAMS, "Invalid channel"
        assert len(data) > 0, "Data is empty"

        if len(data) > self.max_datagram_size - Message.MAX_HEADER_SIZE:
            self._send_split(data, ordered=ordered, sequenced=sequenced, channel=channel)
            return

        self._send(data, reliable=reliable, ordered=ordered, sequenced=sequenced, channel=channel)

    def _send_split(
        self,
        data: bytes | memoryview,
        *,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
    ) -> None:
        """Handles fragmentation of payloads larger than the maximum datagram size."""
        raise NotImplementedError

    def _send(
        self,
        data: bytes | memoryview,
        *,
        reliable: bool,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
        immediate: bool = False,
    ):
        message = Message(data=data, reliability=Reliability.from_flags(reliable, ordered, sequenced), channel=channel)
        if sequenced:
            message.ordering_id = self._ordered_ids[channel]
            message.sequencing_id = self._next_sequencing_id(channel)
        elif ordered:
            message.ordering_id = self._next_ordering_id(channel)
            self._sequenced_ids[channel] = 0

        self._send_queue.append(message)
        self.flush(immediate=immediate)

    def _resend(self, message: Message, immediate: bool = False) -> None:
        """Queue a previously-sent Message for retransmission."""
        if message.resend_handle:
            # cancel resend task after enqueue, as it will be rescheduled when processing the resend queue
            message.resend_handle.cancel()

        self._resend_queue.append(message)
        self.flush(immediate=immediate)

    def _next_reliable_id(self) -> int:
        reliable_id = self._reliable_id
        self._reliable_id = (self._reliable_id + 1) & 0xFFFFFF
        return reliable_id

    def _next_ordering_id(self, channel: int) -> int:
        ordering_id = self._ordered_ids[channel]
        self._ordered_ids[channel] = (self._ordered_ids[channel] + 1) & 0xFFFFFF
        return ordering_id

    def _next_sequencing_id(self, channel: int) -> int:
        sequencing_id = self._sequenced_ids[channel]
        self._sequenced_ids[channel] = (self._sequenced_ids[channel] + 1) & 0xFFFFFF
        return sequencing_id

    def flush(self, immediate: bool = False) -> None:
        """Schedule a datagram flush, either immediately or after a short delay."""
        if immediate:
            if self._flush_handle:
                self._flush_handle.cancel()
            self._flush_handle = self.loop.call_soon(self._flush)
        elif not self._flush_handle:
            self._flush_handle = self.loop.call_later(0.01, self._flush)

    def _flush(self) -> None:
        """Build datagrams from queued and retransmission messages, respecting congestion."""
        time = self.loop.time()
        self._flush_handle = None

        if not self._send_queue and not self._resend_queue:
            return

        datagrams = []

        """
        Retransmission pass (use unACKed bytes as budget)
        """
        bandwidth = self._unacked_bytes
        usage = 0
        while usage < bandwidth:
            datagram_size = 0
            messages = []
            while self._resend_queue:
                message = self._resend_queue[0]
                # message has no resend handle if it has just been acked after enqueue
                if not message.data or not message.resend_handle:
                    self._resend_queue.popleft()
                    continue

                # hit MTU
                message_size = message.header_size + len(message.data)
                if message_size > self.max_datagram_size:
                    break

                message = self._resend_queue.popleft()
                rto = self._cc.get_rto_for_retransmission()
                assert message.resend_handle.cancelled(), "Resend handle should be cleared."
                message.resend_handle = self.loop.call_later(rto, self._resend, message)

                messages.append(message)
                datagram_size += message_size
                usage += message_size

            if messages:
                datagrams.append(messages)
            else:
                break

        """
        New transmission pass (use congestion controller budget)
        """
        bandwidth = self._cc.get_transmission_bandwidth(self._unacked_bytes, False)
        usage = 0
        while usage < bandwidth:
            datagram_size = 0
            messages = []
            while self._send_queue:
                message = self._send_queue[0]
                if not message.data:
                    self._send_queue.popleft()
                    continue

                # hit MTU
                message_size = message.header_size + len(message.data)
                if message_size > self.max_datagram_size:
                    break

                message = self._send_queue.popleft()
                if message.reliability.reliable:
                    message.reliable_id = self._next_reliable_id()
                    rto = self._cc.get_rto_for_retransmission()
                    assert message.resend_handle is None, "Resend handle already set."
                    message.resend_handle = self.loop.call_later(rto, self._resend, message)
                    self._unacked_bytes += message_size

                messages.append(message)
                datagram_size += message_size
                usage += message_size

            if messages:
                datagrams.append(messages)
            else:
                break

        # Send each datagram we've just built
        for index, messages in enumerate(datagrams):
            header = DatagramHeader(
                is_continuous_send=self._bandwidth_exceeded,
                is_in_slow_start=self._cc.is_in_slow_start,
            )
            header.id = self._cc.next_datagram_id()
            header.is_continuous_send = True if index > 0 else False

            stream = ByteStream()
            header.write(stream)
            for message in messages:
                message.write(stream)
                if message.reliability.reliable:
                    self._datagram_history.setdefault(header.id, []).append((message, time))

            self.transport.sendto(stream.data, self._remote_addr)

        # bandwidth is exceeded, flush one more time later
        if self._send_queue:
            self._bandwidth_exceeded = True
            self.flush()

    def handle_datagram(self, data: memoryview, addr: tuple[str, int], recv_time: float) -> list[Message] | None:
        """Process an incoming UDP packet: decode header, handle ACK/NAKs, and extract Messages.

        Args:
            data: Raw packet payload.
            addr: Sender address tuple (host, port).
            recv_time: Timestamp when packet was received.

        Returns:
            If it contains user messages, a list of Message objects; otherwise None.
        """
        stream = ByteStream(data)
        header = DatagramHeader.from_stream(stream)

        if header.is_ack or header.is_nak:
            message_ids = set()
            for _ in range(stream.read_short()):
                singleton = stream.read_bool()
                min_value = stream.read_medium(endian="little")
                max_value = stream.read_medium(endian="little") if not singleton else min_value
                message_ids.update(range(min_value, max_value + 1))

            if header.is_ack:
                self._handle_ack(sorted(message_ids), recv_time)
            else:
                self._handle_nak(sorted(message_ids))

            return None

        skipped = self._cc.on_got_packet(header.id)
        if skipped > 0:
            self._naks.update(range(header.id - skipped - 1, header.id))
            if not self.send_naks_handle:
                self.send_naks_handle = self.loop.call_later(0.01, self._send_naks, addr)

        # Always ACK every packet we get
        self._acks.add(header.id)
        if not self.send_acks_handle:
            self.send_acks_handle = self.loop.call_later(0.01, self._send_acks, addr)

        results = []
        while stream.readable_bytes > 0:
            message = Message.from_stream(stream)
            results.append(message)

        return results

    def _handle_ack(self, datagram_ids: list[int], time: float) -> None:
        """Process incoming ACKs: update RTT, cancel timers, free bytes."""
        for datagram_id in datagram_ids:
            history = self._datagram_history.pop(datagram_id, None)
            if not history:
                continue

            for message, send_time in history:
                rtt = max(0, time - send_time)
                self._cc.on_ack(rtt, self._bandwidth_exceeded, datagram_id)
                if message.resend_handle:
                    message.resend_handle.cancel()
                    message.resend_handle = None
                    self._unacked_bytes -= message.header_size + len(message.data)

    def _handle_nak(self, datagram_ids: list[int]) -> None:
        """Requeue any NAKed messages for immediate retransmission."""
        for datagram_id in datagram_ids:
            history = self._datagram_history.pop(datagram_id, None)
            if not history:
                continue

            for message, _ in history:
                self._resend(message, immediate=True)  # enqueue for immediate resend

    def _send_acks(self, addr: tuple[str, int]) -> None:
        """Batch up pending ACKs into one packet and send."""
        self.send_acks_handle = None
        if not self._acks:
            return

        header = DatagramHeader(is_ack=True)
        stream = ByteStream()
        header.write(stream)
        self._write_range_list(self._acks, stream)
        self.transport.sendto(stream.data, addr)

    def _send_naks(self, addr: tuple[str, int]) -> None:
        """Batch up pending NAKs into one packet and send."""
        self.send_naks_handle = None
        if not self._naks:
            return

        header = DatagramHeader(is_nak=True)
        stream = ByteStream()
        header.write(stream)
        self._write_range_list(self._naks, stream)
        self.transport.sendto(stream.data, addr)
        print("sending nak:", stream.data.hex(sep=" "))

    def _write_range_list(self, numbers: set[int], stream: ByteStream):
        """
        Write a compact list of integer ranges (for ACK/NAK) into the byte stream.

        Consumes entries from `numbers` as they are written, and stops if the range-list itself would exceed the MTU.
        """
        seqs = sorted(int(n) for n in numbers)
        ranges: list[tuple[int, int]] = []
        start = prev = seqs[0]
        for n in seqs[1:]:
            if n == prev + 1:
                prev = n  # extend current range
            else:
                ranges.append((start, prev))
                start = prev = n

        ranges.append((start, prev))

        temp = ByteStream()
        count = 0
        bytes_written = 0
        for min_value, max_value in ranges:
            if bytes_written + 9 > self.max_datagram_size:
                break

            temp.write_bool(min_value == max_value)
            temp.write_medium(min_value, endian="little")
            bytes_written += 4
            if min_value != max_value:
                temp.write_medium(max_value, endian="little")
                bytes_written += 3
            count += 1
            for i in range(min_value, max_value + 1):
                numbers.remove(i)

        stream.write_short(count)
        stream.write(temp.data)

    @property
    def max_datagram_size(self) -> int:
        """Maximum UDP payload we can send (MTU minus DatagramHeader size)."""
        return self._cc.max_mtu - DatagramHeader.SIZE
