import asyncio
import logging

from aiorak.reliability import Reliability, ReliabilityLayer

from . import constants
from .reliability import Message
import socket

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
        constants.ID_DETECT_LOST_CONNECTIONS,
        constants.ID_INVALID_PASSWORD,
        constants.ID_CONNECTION_REQUEST_ACCEPTED,
    }


class Connection(asyncio.DatagramProtocol):
    # TODO: send, recv, keep alive, timeout

    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None
        self.reliability: ReliabilityLayer | None = None
        self.connect_future = self.loop.create_future()
        self.recv_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.external_addr: tuple[str, int] | None = None
        self.remote_addr: list[tuple[str, int]] = [("0.0.0.0", 0)] * constants.MAXIMUM_NUMBER_OF_INTERNAL_IDS
        self.local_addr: list[tuple[str, int]]
        self._fill_local_addr()

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

        messages = self.reliability.handle_datagram(self.transport, view, addr, time)
        if messages is None:
            return

        for message in messages:
            view = memoryview(message.data)
            if is_user_message(view):
                self.recv_queue.put_nowait(message)
            else:
                self.handle_connected_message(view, addr)

    def on_connected_pong(self, ping_time: float, pong_time: float) -> None:
        pass

    async def receive(self) -> tuple[bytes, Reliability]:
        return await self.recv_queue.get()

    def handle_offline_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        raise NotImplementedError

    def handle_connected_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        # TODO: handling ping, pong
        pass

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

    def ping(self, addr: tuple[str, int], *, immediate: bool = False) -> None:
        pass
