"""Unit tests for _connection.py: state machine, handshake handlers, MTU validation."""

import time as _time
from unittest.mock import patch

import pytest

from aiorak._bitstream import BitStream
from aiorak._client import Client
from aiorak._connection import Connection, ConnectionState, _get_local_addresses, _Signal
from aiorak._constants import (
    ID_DETECT_LOST_CONNECTIONS,
    ID_DISCONNECTION_NOTIFICATION,
    ID_INCOMPATIBLE_PROTOCOL_VERSION,
    ID_NEW_INCOMING_CONNECTION,
    ID_NO_FREE_INCOMING_CONNECTIONS,
    ID_OPEN_CONNECTION_REPLY_1,
    ID_OPEN_CONNECTION_REPLY_2,
    ID_OPEN_CONNECTION_REQUEST_1,
    ID_OPEN_CONNECTION_REQUEST_2,
    MAXIMUM_MTU,
    MINIMUM_MTU,
    NUMBER_OF_INTERNAL_IDS,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
    UDP_HEADER_SIZE,
)


class TestConnectionUnit:
    """Connection state machine and handler tests from test_unit_coverage.py."""

    def _make_conn(self, is_server=False, **kwargs):
        return Connection(
            address=("127.0.0.1", 19132),
            guid=0xDEAD,
            is_server=is_server,
            **kwargs,
        )

    def test_get_local_addresses_oserror(self):
        """OSError in getaddrinfo falls back to ['127.0.0.1']."""
        with patch("aiorak._connection._socket.getaddrinfo", side_effect=OSError("fail")):
            result = _get_local_addresses()
        assert result == ["127.0.0.1"]

    def test_connection_repr(self):
        """Connection repr includes class name and state."""
        conn = self._make_conn()
        r = repr(conn)
        assert "Connection" in r
        assert "DISCONNECTED" in r

    async def test_connection_close_idempotent(self):
        """Calling close() twice does not raise."""
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        await conn.close()
        await conn.close()

    def test_build_ocr1_mtu_exhausted(self):
        """All MTU sizes exhausted returns None from _build_open_request_1."""
        conn = self._make_conn()
        conn._mtu_attempt_index = 100
        result = conn._build_open_request_1(1.0)
        assert result is None

    def test_handle_offline_empty(self):
        """Empty offline data returns None."""
        conn = self._make_conn(is_server=True)
        result = conn._handle_offline(b"", 1.0)
        assert result is None

    def test_handle_offline_unknown_id(self):
        """Unknown message ID with valid magic returns None."""
        conn = self._make_conn(is_server=True)
        data = b"\xff" + OFFLINE_MAGIC + b"\x00" * 10
        result = conn._handle_offline(data, 1.0)
        assert result is None

    def test_handle_ocr1_server_side(self):
        """Server handles valid OCR1 and returns OR1, transitioning to CONNECTING."""
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(RAKNET_PROTOCOL_VERSION)
        bs.pad_with_zero_to_byte_length(MINIMUM_MTU - UDP_HEADER_SIZE)
        result = conn._handle_open_request_1(bs.get_data(), 1.0)
        assert result is not None
        assert result[0] == ID_OPEN_CONNECTION_REPLY_1
        assert conn.state == ConnectionState.CONNECTING

    def test_handle_ocr1_bad_magic(self):
        """OCR1 with bad magic returns None."""
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(b"\x00" * 16)
        bs.write_uint8(RAKNET_PROTOCOL_VERSION)
        result = conn._handle_open_request_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr1_wrong_protocol(self):
        """OCR1 with wrong protocol version returns ID_INCOMPATIBLE_PROTOCOL_VERSION."""
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(99)
        result = conn._handle_open_request_1(bs.get_data(), 1.0)
        assert result is not None
        assert result[0] == ID_INCOMPATIBLE_PROTOCOL_VERSION

    def test_handle_or1_bad_magic(self):
        """OR1 with bad magic returns None."""
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(b"\x00" * 16)
        bs.write_uint64(0x1234)
        bs.write_uint8(0)
        bs.write_uint16(1000)
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or1_security_set(self):
        """OR1 with has_security=true returns None (not supported)."""
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(0x1234)
        bs.write_uint8(1)
        bs.write_uint16(1000)
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or1_mtu_out_of_range(self):
        """OR1 with MTU below minimum returns None."""
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(0x1234)
        bs.write_uint8(0)
        bs.write_uint16(10)
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr2_bad_magic(self):
        """OCR2 with bad magic returns None."""
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(0x07)
        bs.write_bytes(b"\x00" * 16)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(1000)
        bs.write_uint64(0xBEEF)
        result = conn._handle_open_request_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr2_mtu_out_of_range(self):
        """OCR2 with MTU below minimum returns None."""
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(0x07)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(10)
        bs.write_uint64(0xBEEF)
        result = conn._handle_open_request_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or2_bad_magic(self):
        """OR2 with bad magic returns None."""
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_2)
        bs.write_bytes(b"\x00" * 16)
        bs.write_uint64(0x1234)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(1000)
        bs.write_bit(False)
        result = conn._handle_open_reply_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_connected_message_empty(self):
        """Empty connected message returns None."""
        conn = self._make_conn()
        result = conn._handle_connected_message(b"", 1.0)
        assert result is None

    def test_handle_detect_lost_connections(self):
        """ID_DETECT_LOST_CONNECTIONS is handled as a no-op (returns None)."""
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        result = conn._handle_connected_message(bytes([ID_DETECT_LOST_CONNECTIONS]), 1.0)
        assert result is None

    def test_handle_new_incoming_connection_malformed(self):
        """Truncated ID_NEW_INCOMING_CONNECTION does not raise."""
        conn = self._make_conn(is_server=True)
        conn.state = ConnectionState.CONNECTING
        conn._handle_new_incoming_connection(bytes([ID_NEW_INCOMING_CONNECTION]), 1.0)

    def test_offline_rejection_client_side(self):
        """Client receiving ID_INCOMPATIBLE_PROTOCOL_VERSION produces DISCONNECT signal."""
        conn = self._make_conn(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = bytes([ID_INCOMPATIBLE_PROTOCOL_VERSION, RAKNET_PROTOCOL_VERSION]) + OFFLINE_MAGIC + b"\x00" * 8
        conn.on_datagram(data, 1.0)
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_offline_rejection_no_free_connections(self):
        """Client receiving ID_NO_FREE_INCOMING_CONNECTIONS produces DISCONNECT signal."""
        conn = self._make_conn(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = bytes([ID_NO_FREE_INCOMING_CONNECTIONS])
        conn.on_datagram(data, 1.0)
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_handshake_retransmit_and_mtu_exhaustion(self):
        """Advancing past retransmit interval exhausts all MTU sizes and disconnects."""
        conn = self._make_conn(is_server=False, mtu_discovery_sizes=(1492, 1200, 576))
        now = 1.0
        conn.start_connect(now)

        for i in range(20):
            now += 2.0
            conn.update(now)

        conn.poll_events()
        assert conn.state == ConnectionState.DISCONNECTED

    def test_disconnect_on_ack_sent(self):
        """DISCONNECTING with _disconnect_on_ack_sent and empty ack_ranges transitions to DISCONNECTED."""
        conn = self._make_conn()
        conn.state = ConnectionState.DISCONNECTING
        conn._disconnect_on_ack_sent = True
        conn._last_recv_time = 1.0
        conn._reliability._ack_ranges = []
        conn.update(1.0)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_graceful_disconnect_flush(self):
        """DISCONNECTING with no pending data transitions to DISCONNECTED."""
        conn = self._make_conn()
        conn.state = ConnectionState.DISCONNECTING
        conn._last_recv_time = 1.0
        assert not conn._reliability.has_pending_data
        conn.update(1.0)
        assert conn.state == ConnectionState.DISCONNECTED
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_disconnection_notification_handling(self):
        """Receiving ID_DISCONNECTION_NOTIFICATION transitions to DISCONNECTING."""
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        conn._handle_connected_message(bytes([ID_DISCONNECTION_NOTIFICATION]), 1.0)
        assert conn.state == ConnectionState.DISCONNECTING
        assert conn._disconnect_on_ack_sent is True

    def test_handshake_timeout(self):
        """Handshake timeout transitions server-side connection to DISCONNECTED."""
        conn = self._make_conn(is_server=True, timeout=5.0)
        conn.state = ConnectionState.CONNECTING
        conn._handshake_start = 1.0
        conn._last_recv_time = 1.0
        conn.update(100.0)
        assert conn.state == ConnectionState.DISCONNECTED
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_connected_timeout(self):
        """Connected connection times out and transitions to DISCONNECTED."""
        conn = self._make_conn(timeout=5.0)
        conn.state = ConnectionState.CONNECTED
        conn._last_recv_time = 1.0
        conn.update(100.0)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_handle_ocr1_via_on_datagram(self):
        """Server-side connection dispatches OCR1 via on_datagram to _handle_offline."""
        conn = self._make_conn(is_server=True)
        conn.state = ConnectionState.DISCONNECTED
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(RAKNET_PROTOCOL_VERSION)
        bs.pad_with_zero_to_byte_length(MINIMUM_MTU - UDP_HEADER_SIZE)
        responses = conn.on_datagram(bs.get_data(), 1.0)
        assert len(responses) == 1
        assert responses[0][0] == ID_OPEN_CONNECTION_REPLY_1

    def test_handle_new_incoming_connection_future_timestamp(self):
        """NIC with future timestamp skips RTT computation but still transitions to CONNECTED."""
        conn = self._make_conn(is_server=True, num_internal_ids=NUMBER_OF_INTERNAL_IDS)
        conn.state = ConnectionState.CONNECTING
        bs = BitStream()
        bs.write_uint8(ID_NEW_INCOMING_CONNECTION)
        bs.write_address("127.0.0.1", 19132)
        for _ in range(NUMBER_OF_INTERNAL_IDS):
            bs.write_address("127.0.0.1", 0)
        bs.write_int64(int(_time.time() * 1000) + 999999999)
        bs.write_int64(int(_time.time() * 1000))
        conn._handle_new_incoming_connection(bs.get_data(), 1.0)
        assert conn.state == ConnectionState.CONNECTED


class TestMTUValidation:
    """MTU bounds checking in handshake handlers."""

    def _make_connection(self, **kwargs):
        return Connection(
            address=("127.0.0.1", 19132),
            guid=1234,
            **kwargs,
        )

    def _build_open_reply_1(self, mtu: int) -> bytes:
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(9999)
        bs.write_uint8(0)
        bs.write_uint16(mtu)
        return bs.get_data()

    def _build_open_request_2(self, mtu: int) -> bytes:
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(mtu)
        bs.write_uint64(5678)
        return bs.get_data()

    def test_reply1_valid_mtu(self):
        """OR1 with valid MTU 1200 is accepted and sets conn.mtu."""
        conn = self._make_connection(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(1200)
        result = conn._handle_open_reply_1(data, _time.monotonic())
        assert result is not None
        assert conn.mtu == 1200

    def test_reply1_mtu_too_low(self):
        """OR1 with MTU below MINIMUM_MTU returns None."""
        conn = self._make_connection(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(MINIMUM_MTU - 1)
        result = conn._handle_open_reply_1(data, _time.monotonic())
        assert result is None

    def test_reply1_mtu_too_high(self):
        """OR1 with MTU above MAXIMUM_MTU returns None."""
        conn = self._make_connection(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(MAXIMUM_MTU + 1)
        result = conn._handle_open_reply_1(data, _time.monotonic())
        assert result is None

    def test_reply1_mtu_at_bounds(self):
        """OR1 with MTU exactly at MINIMUM_MTU and MAXIMUM_MTU is accepted."""
        for mtu in (MINIMUM_MTU, MAXIMUM_MTU):
            conn = self._make_connection(is_server=False)
            conn.state = ConnectionState.CONNECTING
            data = self._build_open_reply_1(mtu)
            result = conn._handle_open_reply_1(data, _time.monotonic())
            assert result is not None, f"MTU {mtu} should be accepted"

    def test_request2_valid_mtu(self):
        """OCR2 with valid MTU 1200 is accepted and sets conn.mtu."""
        conn = self._make_connection(is_server=True)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(1200)
        result = conn._handle_open_request_2(data, _time.monotonic())
        assert result is not None
        assert conn.mtu == 1200

    def test_request2_mtu_too_low(self):
        """OCR2 with MTU below MINIMUM_MTU returns None."""
        conn = self._make_connection(is_server=True)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(MINIMUM_MTU - 1)
        result = conn._handle_open_request_2(data, _time.monotonic())
        assert result is None

    def test_request2_mtu_too_high(self):
        """OCR2 with MTU above MAXIMUM_MTU returns None."""
        conn = self._make_connection(is_server=True)
        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(MAXIMUM_MTU + 1)
        result = conn._handle_open_request_2(data, _time.monotonic())
        assert result is None


class TestClientContextManager:
    """Client async context manager protocol."""

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Client supports async with and calls close() on exit."""
        client = Client(("127.0.0.1", 19132))
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True

        client.close = mock_close

        async with client:
            pass

        assert closed

    @pytest.mark.asyncio
    async def test_context_manager_on_exception(self):
        """Client.close() is called even if the body raises an exception."""
        client = Client(("127.0.0.1", 19132))
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True

        client.close = mock_close

        with pytest.raises(RuntimeError):
            async with client:
                raise RuntimeError("test error")

        assert closed
