import asyncio
import logging

from aiorak.reliability import ReliabilityLayer, Reliability

logger = logging.getLogger("aiorak.connection")
from . import constants


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


class Connection(asyncio.DatagramProtocol):
    # TODO: send, recv, keep alive, timeout

    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None
        self.reliability: ReliabilityLayer | None = None
        self.connect_future = self.loop.create_future()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        view = memoryview(data)
        offline_msg = is_offline_message(view)
        if offline_msg:
            self.handle_offline_message(view, addr)
        else:
            results = self.reliability.handle_datagram(view, addr)
            if results is None:
                return

            for data, reliability in results:
                self.handle_connected_message(memoryview(data), addr, reliability)

    def handle_offline_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        raise NotImplementedError

    def handle_connected_message(self, data: memoryview, addr: tuple[str, int], reliability: Reliability) -> None:
        raise NotImplementedError
