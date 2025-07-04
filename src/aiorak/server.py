import asyncio
import typing
import uuid

from aiorak.stream import ByteStream

from . import constants
from .connection import Connection


class ServerConnection(Connection):
    pass


class Server(asyncio.DatagramProtocol):
    def __init__(
        self,
        handler: typing.Callable[[ServerConnection], typing.Awaitable[None]],
        local_addr: tuple[str, int] | None = None,
    ):
        self.loop = asyncio.get_event_loop()
        self.transport: asyncio.DatagramTransport | None = None
        self.offline_ping_message: bytes | None = None
        self.handler = handler
        self.binding_addr = local_addr
        self.close_future = self.loop.create_future()
        self.guid = uuid.uuid4().int >> 64
        self._protocol_version = constants.RAKNET_PROTOCOL_VERSION

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        mv = memoryview(data)
        if mv[0] in {constants.ID_UNCONNECTED_PING, constants.ID_UNCONNECTED_PING_OPEN_CONNECTIONS}:
            if not self.offline_ping_message:
                return

            stream = ByteStream()
            stream.write_byte(constants.ID_UNCONNECTED_PONG)
            stream.write_long(int(self.loop.time() * 1000))
            stream.write_long(self.guid)
            stream.write(constants.OFFLINE_MESSAGE_DATA_ID)
            stream.write(self.offline_ping_message)
            self.transport.sendto(stream.data, addr)

    async def bind(self):
        await self.loop.create_datagram_endpoint(lambda: self, local_addr=self.binding_addr)

    async def wait_closed(self):
        await self.close_future

    async def __aenter__(self):
        await self.bind()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self.close_future.done():
            self.close_future.set_result(None)
        if self.transport:
            self.transport.close()
        return False


def serve(handler: typing.Callable[[ServerConnection], typing.Awaitable[None]], host: str, port: int) -> Server:
    return Server(handler, (host, port))
