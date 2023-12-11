from enum import Enum


class ConnectionState(Enum):
    PENDING = 0,
    CONNECTING = 1,
    CONNECTED = 2,
    DISCONNECTING = 3,
    DISCONNECTING_SILENTLY = 4,
    DISCONNECTED = 5,
    NOT_CONNECTED = 6,
