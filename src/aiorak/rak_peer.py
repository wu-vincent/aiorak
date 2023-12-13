import asyncio
import uuid
from typing import Optional, Tuple, List, Union

from aiorak.protocol import RakNetProtocol
from aiorak.types import ConnectionState, SystemAddress, Packet, SystemIdentifier


class RakPeer:
    def __init__(self):
        self._max_connections = 0

    async def start(self, max_connections: int, local_addr: Optional[Tuple[str, int]] = None) -> None:
        """
        Starts the network connection and opens the port with a specified maximum number of connections.

        Args:
            max_connections (int): The maximum number of connections between this instance of ``RakPeer`` and another
                                   instance of ``RakPeer``. A pure client would set this to 1. A pure server would set
                                   it to the number of allowed clients. A hybrid would set it to the sum of both types
                                   of connections.
            local_addr (Optional[Tuple[str, int]]): The local address to bind to.
        """
        self.max_connections = max_connections

        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            protocol_factory=lambda: RakNetProtocol(),
            local_addr=local_addr,
        )

    @property
    def max_connections(self) -> int:
        """
        Returns:
            int: The maximum number of connections allowed.
        """
        return self._max_connections

    @max_connections.setter
    def max_connections(self, max_connections: int) -> None:
        """
        Sets the maximum number of connections allowed. If the number of connections is less than the number of current
        connections, no more connections will be allowed.

        Args:
            max_connections (int): The maximum number of connections allowed.
        """
        assert max_connections > 0, ValueError("Invalid max_connections")
        self._max_connections = max_connections

    @property
    def num_connections(self) -> int:
        """
        Returns: The number of connections exists.
        """
        raise NotImplementedError

    async def connect(self, remote_addr: Tuple[str, int], max_attempts: int = 6, timeout: int = 0) -> None:
        """
        Connects to the specified remote endpoint.

        Args:
            remote_addr (Tuple[str, int]): The remote endpoint to connect to, in the form (host, port).
            max_attempts (int): The maximum number of attempts to connect to the remote endpoint. Defaults to 6.
            timeout (int): The timeout in seconds to elapse before dropping the connection if a reliable message could
                           not be sent. Defaults to 0.

        Raises:
            ConnectionError: If the connection is not established after ``max_attempts`` number of attempts.
        """
        raise NotImplementedError

    async def shutdown(self, wait_time: Optional[float] = None) -> None:
        """
        Shuts down the peer and closes all connections.

        Args:
            wait_time (Optional[float]): The maximum time in seconds to wait for all remaining messages to go out.
                                         If None, it does not wait at all. Defaults to None.
        """
        raise NotImplementedError

    @property
    def active(self) -> bool:
        """
        Returns: ``true`` if the peer is active, ``false`` otherwise
        """
        raise NotImplementedError

    @property
    def connection_list(self) -> List[SystemAddress]:
        """
        Returns: A list of all connected systems.
        """
        raise NotImplementedError

    async def send(self, data: Union[bytes, List[bytes]], target: Union[SystemAddress, SystemIdentifier]) -> int:
        """
        Sends data or a list of data to the specified system.

        Args:
            data (Union[bytes, List[bytes]]): The data or the list of data to send.
            target (Union[SystemAddress, SystemIdentifier]): The remote system to send to.

        Returns:
            int: The receipt number that identifies the data to be sent.
        """
        raise NotImplementedError

    async def broadcast(
        self, data: Union[bytes, List[bytes]], excludes: List[Union[SystemAddress, SystemIdentifier]]
    ) -> int:
        """
        Broadcasts the data or a list of data to all the connected systems.

        Args:
            data (Union[bytes, List[bytes]]): The data or the list of data to send.
            excludes: The remote systems not to send data to.

        Returns:
            int: The receipt number that identifies the data to be broadcast.
        """
        raise NotImplementedError

    async def receive(self) -> Optional[Packet]:
        """
        Receives the incoming packet from the message queue.

        Returns:
            Optional[Packet]: The received packet or ``None`` if there is no packet.
        """
        raise NotImplementedError

    async def close(self, target: Union[SystemAddress, SystemIdentifier]) -> None:
        """
        Closes the connection to a specified system. If this system initiated the connection, it will disconnect.
        If the connection was initiated by the target system, that system will be disconnected and removed.

        Args:
            target: The system to close the connection to.
        """
        raise NotImplementedError

    def get_connection_state(self, target: Union[SystemAddress, SystemIdentifier]) -> ConnectionState:
        """
        Gets the connection state of a specified system, whether it is connected, disconnected, connecting or etc.

        Returns:
            ConnectionState: The connection state.
        """
        raise NotImplementedError
