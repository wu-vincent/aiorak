"""Tests for the aiorak exception hierarchy and type enums."""

import pytest

import aiorak
from aiorak._exceptions import (
    ConnectionClosedError,
    ConnectionRejectedError,
    HandshakeError,
    ProtocolError,
    RakNetError,
    RakNetTimeoutError,
)


class TestExceptionHierarchy:
    """Verify dual inheritance so ``except RakNetError`` catches everything
    and existing stdlib ``except`` clauses still work."""

    def test_all_inherit_from_raknet_error(self):
        """All custom exceptions inherit from RakNetError."""
        for cls in (HandshakeError, ConnectionRejectedError, ConnectionClosedError, ProtocolError, RakNetTimeoutError):
            assert issubclass(cls, RakNetError)

    def test_handshake_error_is_connection_error(self):
        """HandshakeError is a ConnectionError for stdlib compat."""
        assert issubclass(HandshakeError, ConnectionError)

    def test_connection_rejected_is_connection_refused(self):
        """ConnectionRejectedError is both ConnectionRefusedError and HandshakeError."""
        assert issubclass(ConnectionRejectedError, ConnectionRefusedError)
        assert issubclass(ConnectionRejectedError, HandshakeError)

    def test_connection_closed_is_runtime_error(self):
        """ConnectionClosedError is a RuntimeError for stdlib compat."""
        assert issubclass(ConnectionClosedError, RuntimeError)

    def test_protocol_error_is_value_error(self):
        """ProtocolError is a ValueError for stdlib compat."""
        assert issubclass(ProtocolError, ValueError)

    def test_raknet_timeout_is_timeout_error(self):
        """RakNetTimeoutError is a TimeoutError for stdlib compat."""
        assert issubclass(RakNetTimeoutError, TimeoutError)

    def test_except_raknet_error_catches_all(self):
        """Raising any custom exception is caught by except RakNetError."""
        for cls in (HandshakeError, ConnectionRejectedError, ConnectionClosedError, ProtocolError, RakNetTimeoutError):
            with pytest.raises(RakNetError):
                raise cls("test")

    def test_except_stdlib_still_works(self):
        """Custom exceptions are caught by their stdlib base classes."""
        with pytest.raises(RuntimeError):
            raise ConnectionClosedError("closed")
        with pytest.raises(ConnectionRefusedError):
            raise ConnectionRejectedError("rejected")
        with pytest.raises(TimeoutError):
            raise RakNetTimeoutError("timeout")
        with pytest.raises(ValueError):
            raise ProtocolError("bad packet")
        with pytest.raises(ConnectionError):
            raise HandshakeError("handshake failed")


class TestExportsInPackage:
    """Verify new exceptions are accessible from the top-level ``aiorak`` package."""

    def test_new_classes_exported(self):
        """ConnectionRejectedError and RakNetTimeoutError are in the aiorak namespace."""
        assert aiorak.ConnectionRejectedError is ConnectionRejectedError
        assert aiorak.RakNetTimeoutError is RakNetTimeoutError


class TestConnectionClosedErrorRaised:
    """Verify Connection.send raises ConnectionClosedError on closed connections."""

    def test_send_on_closed_connection(self):
        """Sending on a connection with _closed=True raises ConnectionClosedError."""
        from aiorak._connection import Connection

        conn = Connection(("127.0.0.1", 19132), guid=1)
        conn._closed = True
        with pytest.raises(ConnectionClosedError):
            conn.send(b"hello")

    def test_send_on_disconnected_connection(self):
        """Sending on a DISCONNECTED connection via _send raises ConnectionClosedError."""
        from aiorak._connection import Connection

        conn = Connection(("127.0.0.1", 19132), guid=1)
        with pytest.raises(ConnectionClosedError):
            conn._send(b"hello")
