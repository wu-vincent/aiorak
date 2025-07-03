import asyncio

from .connection import Connection
from .constants import MAXIMUM_MTU_SIZE, ID_OPEN_CONNECTION_REQUEST_1, OFFLINE_MESSAGE_DATA_ID, RAKNET_PROTOCOL_VERSION, \
    UDP_HEADER_SIZE
from .stream import ByteStream


class ClientConnection(Connection):
    def __init__(self):
        super().__init__()
        self._remote_addr: tuple[str, int] | None = None
        self._connect_future = self.loop.create_future()

    async def connect(self, remote_addr: tuple[str, int], max_mtu: int = MAXIMUM_MTU_SIZE, attempt_interval=0.5):
        await self.loop.create_datagram_endpoint(lambda: self, None, remote_addr)
        for mtu_size in [max_mtu, 1200, 576]:
            out = ByteStream()
            out.write_byte(ID_OPEN_CONNECTION_REQUEST_1)
            out.write(OFFLINE_MESSAGE_DATA_ID)
            out.write_byte(RAKNET_PROTOCOL_VERSION)
            out.write(b"\x00" * (mtu_size - UDP_HEADER_SIZE))

            for attempt in range(4):
                self.transport.sendto(out.data)
                await asyncio.sleep(attempt_interval)
                if self._remote_addr is not None:
                    await asyncio.wait_for(self._connect_future, timeout=5)
                    return

        raise TimeoutError


async def connect(host: str, port: int) -> ClientConnection:
    client = ClientConnection()
    await client.connect((host, port))
    return client
