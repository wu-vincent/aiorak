import asyncio
import uuid

import aiorak
from . import constants
from .connection import Connection
from .stream import ByteStream
from .reliability import ReliabilityLayer


class ClientConnection(Connection):
    def __init__(self, protocol_version=constants.RAKNET_PROTOCOL_VERSION):
        super().__init__()
        self._open_future = self.loop.create_future()
        self._connect_future = self.loop.create_future()
        self.guid = uuid.uuid4().int >> 64
        self._protocol_version = protocol_version

    async def connect(
        self,
        remote_addr: tuple[str, int],
        *,
        max_mtu: int = constants.MAXIMUM_MTU_SIZE,
        attempt_interval=0.5,
        timeout=10,
    ):
        await self.loop.create_datagram_endpoint(lambda: self, None, remote_addr)
        for mtu_size in [max_mtu, 1200, 576]:
            out = ByteStream()
            out.write_byte(constants.ID_OPEN_CONNECTION_REQUEST_1)
            out.write(constants.OFFLINE_MESSAGE_DATA_ID)
            out.write_byte(self._protocol_version)
            out.write(b"\x00" * (mtu_size - constants.UDP_HEADER_SIZE))

            for attempt in range(4):
                self.transport.sendto(out.data)
                try:
                    await asyncio.wait_for(asyncio.shield(self._open_future), timeout=attempt_interval)
                    if self._open_future.exception():
                        raise self._open_future.exception()

                except asyncio.TimeoutError:
                    continue

        await asyncio.wait_for(self._connect_future, timeout=timeout)

    def handle_offline_message(self, data: memoryview, addr: tuple[str, int]) -> bool:
        connection_errors = {
            constants.ID_INCOMPATIBLE_PROTOCOL_VERSION: "Incompatible protocol version",
            constants.ID_CONNECTION_ATTEMPT_FAILED: "Connection attempt failed",
            constants.ID_NO_FREE_INCOMING_CONNECTIONS: "No free incoming connections",
            constants.ID_CONNECTION_BANNED: "Connection banned by server",
            constants.ID_ALREADY_CONNECTED: "Already connected to server",
            constants.ID_IP_RECENTLY_CONNECTED: "IP recently connected",
        }
        match data[0]:
            case constants.ID_OPEN_CONNECTION_REPLY_1:
                self._handle_open_connection_reply_1(data, addr)
            case constants.ID_OPEN_CONNECTION_REPLY_2:
                self._handle_open_connection_reply_2(data, addr)
            case constants.ID_INCOMPATIBLE_PROTOCOL_VERSION:
                if not self._open_future.done():
                    self._open_future.set_exception(ConnectionRefusedError("Incompatible protocol version"))
            case message_id if message_id in connection_errors:
                if not self._open_future.done():
                    self._open_future.set_exception(ConnectionRefusedError(connection_errors[message_id]))
            case _:
                raise NotImplementedError(f"Unhandled offline message: {data.hex(sep=' ')}")

        return True

    def _handle_open_connection_reply_1(self, data: memoryview, addr: tuple[str, int]) -> None:
        if self._open_future.done():
            return

        self._open_future.set_result(None)

        bs = ByteStream(data)
        bs.skip_bytes(1)
        bs.skip_bytes(len(constants.OFFLINE_MESSAGE_DATA_ID))
        bs.skip_bytes(8)  # server guid
        has_security = bs.read_bool()
        mtu_size = bs.read_short()
        assert has_security is False, "Security is not supported yet"

        out = ByteStream()
        out.write_byte(constants.ID_OPEN_CONNECTION_REQUEST_2)
        out.write(constants.OFFLINE_MESSAGE_DATA_ID)
        out.write_bool(has_security)
        out.write_address(addr)
        out.write_short(mtu_size)
        out.write_long(self.guid)
        self.transport.sendto(out.data, addr)

    def _handle_open_connection_reply_2(self, data: memoryview, addr: tuple[str, int]) -> None:
        if self.reliability is not None:
            return

        bs = ByteStream(data)
        bs.skip_bytes(1)
        bs.skip_bytes(len(constants.OFFLINE_MESSAGE_DATA_ID))
        sever_guid = bs.read_long()
        client_addr = bs.read_address()
        mtu_size = bs.read_short()
        do_security = bs.read_bool()
        assert do_security is False, "Security is not supported yet"

        out = ByteStream()
        out.write_byte(constants.ID_CONNECTION_REQUEST)
        out.write_long(self.guid)
        out.write_long(int(self.loop.time() * 1000))
        out.write_bool(False)  # security
        self.reliability = ReliabilityLayer(addr, mtu_size)
        self.reliability.send(self.transport, out.data, reliable=True)


async def connect(host: str, port: int, **kwargs) -> ClientConnection:
    client = ClientConnection(**kwargs)
    await client.connect((host, port))
    return client
