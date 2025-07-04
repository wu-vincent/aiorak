import builtins


class RakError(Exception):
    """Base class for all aiorak errors."""


class ConnectionError(RakError, builtins.ConnectionError):
    """Something went wrong establishing the connection."""


class DisconnectionError(RakError):
    """Remote peer cleanly closed the connection."""


class TimeoutError(RakError, builtins.TimeoutError):
    """An operation timed out."""
