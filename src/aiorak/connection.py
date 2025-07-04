import asyncio
import enum
import logging
import socket
from collections import deque

from aiorak.reliability import Reliability, ReliabilityLayer
from aiorak.stream import ByteStream

from . import constants
from .reliability import Message

logger = logging.getLogger("aiorak.connection")


def is_offline_message(data: memoryview) -> bool:
    match data[0]:
        case constants.ID_UNCONNECTED_PING | constants.ID_UNCONNECTED_PING_OPEN_CONNECTIONS:
            offset = 1 + 8
        case constants.ID_UNCONNECTED_PONG:
            offset = 1 + 8 + 8
        case (
            constants.ID_OPEN_CONNECTION_REPLY_1
            | constants.ID_OPEN_CONNECTION_REPLY_2
            | constants.ID_OPEN_CONNECTION_REQUEST_1
            | constants.ID_OPEN_CONNECTION_REQUEST_2
            | constants.ID_CONNECTION_ATTEMPT_FAILED
            | constants.ID_NO_FREE_INCOMING_CONNECTIONS
            | constants.ID_CONNECTION_BANNED
            | constants.ID_ALREADY_CONNECTED
            | constants.ID_IP_RECENTLY_CONNECTED
        ):
            offset = 1
        case constants.ID_INCOMPATIBLE_PROTOCOL_VERSION:
            offset = 2
        case _:
            return False

    return data[offset : offset + 16] == constants.OFFLINE_MESSAGE_DATA_ID


def is_user_message(data: memoryview) -> bool:
    return data[0] not in {
        constants.ID_CONNECTION_REQUEST,
        constants.ID_NEW_INCOMING_CONNECTION,
        constants.ID_CONNECTED_PONG,
        constants.ID_CONNECTED_PING,
        constants.ID_DISCONNECTION_NOTIFICATION,
        constants.ID_INVALID_PASSWORD,
        constants.ID_CONNECTION_REQUEST_ACCEPTED,
    }


class State(enum.IntEnum):
    INVALID, CONNECTING, CONNECTED, DISCONNECTING, DISCONNECTED = range(5)


class Connection(asyncio.DatagramProtocol):
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None
        self.reliability: ReliabilityLayer | None = None
        self.connect_future = self.loop.create_future()
        self.close_future = self.loop.create_future()
        self.recv_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.external_addr: tuple[str, int] | None = None
        self.remote_addr: list[tuple[str, int]] = [("0.0.0.0", 0)] * constants.MAXIMUM_NUMBER_OF_INTERNAL_IDS
        self.local_addr: list[tuple[str, int]]
        self._fill_local_addr()
        self._ping: deque[float] = deque(maxlen=5)
        self._lowest_ping: float = None
        self._keep_alive_handle: asyncio.TimerHandle | None = None
        self._timeout_handle: asyncio.TimerHandle | None = None
        self.state = State.INVALID

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport
        _, port = transport.get_extra_info("sockname")
        for i, addr in enumerate(self.local_addr):
            if addr[0] == "0.0.0.0":
                break
            self.local_addr[i] = (addr[0], port)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        time = self.loop.time()
        view = memoryview(data)
        offline_msg = is_offline_message(view)
        if offline_msg:
            self.handle_offline_message(view, addr)
            return

        if self._timeout_handle:
            self._timeout_handle.cancel()
        self._timeout_handle = self.loop.call_later(self.reliability.timeout, self._timeout)
        messages = self.reliability.handle_datagram(view, addr, time)
        if messages is None:
            return

        for message in messages:
            view = memoryview(message.data)
            if is_user_message(view):
                print(message.data.hex(sep=" "))
                self.recv_queue.put_nowait(message)
            else:
                self.handle_connected_message(view, addr)

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
        if self.state not in {State.CONNECTING, State.CONNECTED}:
            return

        self.reliability.send(
            data,
            reliable=reliable,
            ordered=ordered,
            sequenced=sequenced,
            channel=channel,
            immediate=immediate,
        )

        if self.connect_future.done() and reliable:
            # Reliable sends must occur at least once every timeout /2 to notice disconnects
            if self._keep_alive_handle:
                self._keep_alive_handle.cancel()
            self._keep_alive_handle = self.loop.call_later(self.reliability.timeout / 2, self._keep_alive)

    async def receive(self) -> tuple[bytes, Reliability]:
        message = await self.recv_queue.get()
        return message.data, message.reliability

    def ping(self, *, reliable: bool = False, immediate: bool = False) -> None:
        if self.reliability is None:
            return

        stream = ByteStream()
        stream.write_byte(constants.ID_CONNECTED_PING)
        stream.write_long(int(self.loop.time() * 1000))
        self.send(stream.data, reliable=reliable, immediate=immediate)

    def handle_offline_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        raise NotImplementedError

    def handle_connected_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        match data[0]:
            case constants.ID_CONNECTED_PING:
                stream = ByteStream(data)
                stream.skip_bytes(1)
                ping_time = stream.read_long()

                out = ByteStream()
                out.write_byte(constants.ID_CONNECTED_PONG)
                out.write_long(ping_time)
                out.write_long(int(self.loop.time() * 1000))
                self.send(out.data, reliable=False)

            case constants.ID_CONNECTED_PONG:
                stream = ByteStream(data)
                stream.skip_bytes(1)
                ping_time = stream.read_long()
                pong_time = stream.read_long()
                self.on_connected_pong(ping_time / 1000.0, pong_time / 1000.0)

            case constants.ID_DISCONNECTION_NOTIFICATION:
                self.state = State.DISCONNECTING
                self.loop.create_task(self._wait_for_pending_acks())

    def on_connected_pong(self, ping_time: float, pong_time: float) -> None:
        ping = max(0, self.loop.time() - ping_time)
        self._ping.append(ping)
        if self._lowest_ping is None or ping < self._lowest_ping:
            self._lowest_ping = ping

    def _fill_local_addr(self) -> None:
        self.local_addr = [("0.0.0.0", 0)] * constants.MAXIMUM_NUMBER_OF_INTERNAL_IDS
        hostname = socket.gethostname()
        addrinfo = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_DGRAM)
        i = 0
        for family, socktype, proto, canonname, sockaddr in addrinfo:
            if family in (socket.AF_INET, socket.AF_INET6):
                self.local_addr[i] = (sockaddr[0], 0)
                i += 1
                if i >= constants.MAXIMUM_NUMBER_OF_INTERNAL_IDS:
                    break

    def _keep_alive(self) -> None:
        # If no reliable packets are waiting for an ack, do a one byte reliable send so that disconnections are
        if not self.reliability.resend_handles:
            self.ping(reliable=True, immediate=True)
            self._keep_alive_handle = self.loop.call_later(self.reliability.timeout / 2, self._keep_alive)
        else:
            self._keep_alive_handle = self.loop.call_later(0.1, self._keep_alive)

    def _timeout(self) -> None:
        if self.close_future.done():
            return

        if self.state == State.DISCONNECTING:
            self.close_future.set_result(None)
        else:
            self.close_future.set_exception(TimeoutError("Connection timed out"))

        self.state = State.DISCONNECTED

    async def _wait_for_pending_acks(self) -> None:
        if self.close_future.done():
            return

        while self.reliability._acks:
            await asyncio.sleep(0.1)

        self.close_future.set_result(None)
        self.state = State.DISCONNECTED
