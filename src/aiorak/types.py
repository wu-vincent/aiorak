from dataclasses import dataclass
from enum import Enum


@dataclass
class SystemAddress:
    address: str
    port: int


@dataclass
class SystemIdentifier:
    id: int


@dataclass
class Packet:
    system_address: SystemAddress
    data: bytes


class ConnectionState(Enum):
    PENDING = 0
    CONNECTING = 1
    CONNECTED = 2
    DISCONNECTING = 3
    DISCONNECTING_SILENTLY = 4
    DISCONNECTED = 5
    NOT_CONNECTED = 6
