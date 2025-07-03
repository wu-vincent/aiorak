import asyncio
import logging

logger = logging.getLogger("aiorak.connection")


class Connection(asyncio.DatagramProtocol):
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        print("recv:", data.hex(sep=' '))
        if self.handle_offline_message(data, addr):
            return

        self.handle_message(data, addr)

    def handle_offline_message(self, data: bytes, addr: tuple[str, int]) -> bool:
        raise NotImplementedError

    def handle_message(self, data: bytes, addr: tuple[str, int]) -> None:
        pass
