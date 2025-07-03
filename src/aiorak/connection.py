import asyncio
import logging

from aiorak.reliability import ReliabilityLayer

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
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None
        self.reliability: ReliabilityLayer | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        view = memoryview(data)
        if is_offline_message(view) and self.handle_offline_message(view, addr):
            return

        self.handle_message(view, addr)

    def handle_offline_message(self, data: memoryview, addr: tuple[str, int]) -> bool:
        raise NotImplementedError

    def handle_message(self, data: memoryview, addr: tuple[str, int]) -> None:
        pass
