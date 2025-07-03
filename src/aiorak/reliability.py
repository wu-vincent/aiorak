import asyncio
import enum
import typing
from collections import deque
from dataclasses import dataclass

from .constants import NUMBER_OF_ORDERED_STREAMS
from .sliding_window import SlidingWindow
from . import constants


class Reliability(enum.IntEnum):
    UNRELIABLE = 0
    UNRELIABLE_SEQUENCED = 1
    RELIABLE = 2
    RELIABLE_ORDERED = 3
    RELIABLE_SEQUENCED = 4

    @property
    def is_reliable(self) -> bool:
        return self in {
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
        }

    @property
    def is_sequenced(self) -> bool:
        return self in {Reliability.UNRELIABLE_SEQUENCED, Reliability.RELIABLE_SEQUENCED}

    @property
    def is_ordered(self) -> bool:
        return self is Reliability.RELIABLE_ORDERED or self.is_sequenced

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
class InternalPacket:
    HEADER_SIZE: typing.ClassVar[int] = 23  # worse case scenario
    data: bytes
    reliability: Reliability
    reliable_id: int | None = None
    ordering_id: int | None = None
    sequencing_id: int | None = None
    channel: int = 0
    split_id: int | None = None
    split_index: int | None = None
    split_count: int | None = None


@dataclass(slots=True)
class Datagram:
    HEADER_SIZE: typing.ClassVar[int] = 4  # flag (1 byte) + datagram_id (3 bytes)
    is_valid: bool
    is_ack: bool
    is_nak: bool
    is_continuous_send: bool
    datagram_id: int
    payloads: list[InternalPacket]


class ReliabilityLayer:
    def __init__(self, mtu: int):
        assert mtu <= constants.MAXIMUM_MTU_SIZE, "MTU is too large"
        self.loop = asyncio.get_event_loop()
        self._cc = SlidingWindow(mtu - constants.UDP_HEADER_SIZE)
        self._send_queue: deque[InternalPacket] = deque()
        self._resend_queue: deque[InternalPacket] = deque()  # TODO: find a better data structure
        self._ordered_ids = [0] * NUMBER_OF_ORDERED_STREAMS
        self._sequenced_ids = [0] * NUMBER_OF_ORDERED_STREAMS
        self._flush_handle: asyncio.TimerHandle | None = None

    def send(
        self,
        transport: asyncio.DatagramTransport,
        data: bytes | memoryview,
        *,
        reliable: bool,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
    ) -> None:
        assert 0 <= channel < constants.NUMBER_OF_ORDERED_STREAMS, "Invalid channel"
        assert len(data) > 0, "Data is empty"

        if len(data) > self._cc.max_mtu - Datagram.HEADER_SIZE - InternalPacket.HEADER_SIZE:
            self._send_split(transport, data, ordered=ordered, sequenced=sequenced, channel=channel)
            return

        self._send_message(transport, data, reliable=reliable, ordered=ordered, sequenced=sequenced, channel=channel)

    def _send_message(
        self,
        transport: asyncio.DatagramTransport,
        data: bytes | memoryview,
        *,
        reliable: bool,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
    ):
        message = InternalPacket(
            data=data, reliability=Reliability.from_flags(reliable, ordered, sequenced), channel=channel
        )
        if sequenced:
            message.ordering_id = self._ordered_ids[channel]
            message.sequencing_id = self._next_sequencing_id(channel)
        elif ordered:
            message.ordering_id = self._next_ordering_id(channel)
            self._sequenced_ids[channel] = 0

        self._send_queue.append(message)

        if self._flush_handle is None:
            self._flush_handle = self.loop.call_later(0.01, self.flush, transport)

    def _send_split(
        self,
        transport: asyncio.DatagramTransport,
        data: bytes | memoryview,
        *,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
    ) -> None:
        raise NotImplementedError

    def _next_ordering_id(self, channel: int) -> int:
        ordering_id = self._ordered_ids[channel]
        self._ordered_ids[channel] = (self._ordered_ids[channel] + 1) & 0xFFFFFF
        return ordering_id

    def _next_sequencing_id(self, channel: int) -> int:
        sequencing_id = self._sequenced_ids[channel]
        self._sequenced_ids[channel] = (self._sequenced_ids[channel] + 1) & 0xFFFFFF
        return sequencing_id

    def flush(self, transport: asyncio.DatagramTransport) -> None:
        self._flush_handle = None

        if not self._send_queue and not self._resend_queue:
            return
