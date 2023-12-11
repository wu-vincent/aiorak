import asyncio
from typing import Optional

from aiorak.protocol import RakNetProtocol
from aiorak.types import ConnectionState


class RakPeer:
    def __init__(self):
        self.max_connections = 0
        self.transport = None
        self.protocol = None

    async def start(self, max_connections: int, local_addr: Optional = None) -> None:
        assert max_connections > 0, ValueError("Invalid max_connections")
        self.max_connections = max_connections

        loop = asyncio.get_running_loop()
        self.transport, self.protocol = await loop.create_datagram_endpoint(
            protocol_factory=lambda: RakNetProtocol(),
            local_addr=local_addr,
        )

    async def connect(self, remote_addr) -> None:
        raise NotImplementedError

    def get_connection_state(self, addr) -> ConnectionState:
        raise NotImplementedError
