"""Tests for production hardening: sliding window, split tracker eviction,
channel validation, MTU validation, range list cap, and client context manager."""

import time

import pytest

from aiorak._bitstream import BitStream
from aiorak._client import Client
from aiorak._congestion import CongestionController
from aiorak._connection import Connection
from aiorak._constants import (
    DATAGRAM_MESSAGE_ID_ARRAY_LENGTH,
    DEFAULT_RECEIVED_PACKET_QUEUE_SIZE,
    MAXIMUM_MTU,
    MINIMUM_MTU,
    NUMBER_OF_ORDERED_STREAMS,
    SEQ_NUM_MAX,
)
from aiorak._reliability import ReliabilityLayer, _ReceivedWindow
from aiorak._types import Reliability
from aiorak._wire import MessageFrame, decode_range_list, encode_datagram

# -----------------------------------------------------------------------
# _ReceivedWindow tests
# -----------------------------------------------------------------------


class TestReceivedWindow:
    def test_add_and_contains(self):
        w = _ReceivedWindow(size=512)
        assert 0 not in w
        w.add(0)
        assert 0 in w

    def test_sequential_add(self):
        w = _ReceivedWindow(size=8)
        for i in range(8):
            w.add(i)
        for i in range(8):
            assert i in w

    def test_window_advance(self):
        w = _ReceivedWindow(size=8)
        # Fill the window
        for i in range(8):
            w.add(i)
        # Add beyond window — forces advance
        w.add(10)
        assert 10 in w
        # Old entries below new base should be considered received
        assert 0 in w  # below base, so treated as received
        assert 2 in w  # also below base

    def test_far_ahead_advances_window(self):
        w = _ReceivedWindow(size=8)
        w.add(0)
        w.add(1)
        # Jump far ahead
        w.add(100)
        assert 100 in w
        # Old values below base are considered received
        assert 0 in w
        assert 50 in w  # between old base and new base, below base

    def test_wrap_around_seq_num_max(self):
        w = _ReceivedWindow(size=512)
        # Start near the end of the 24-bit range
        base = SEQ_NUM_MAX - 5
        for i in range(10):
            seq = (base + i) & SEQ_NUM_MAX
            w.add(seq)
        # Check they're all there
        for i in range(10):
            seq = (base + i) & SEQ_NUM_MAX
            assert seq in w

    def test_behind_base_is_received(self):
        w = _ReceivedWindow(size=8)
        # Advance the window
        for i in range(20):
            w.add(i)
        # Anything far behind should be considered received
        assert 0 in w
        assert 5 in w

    def test_default_size(self):
        w = _ReceivedWindow()
        assert w._size == DEFAULT_RECEIVED_PACKET_QUEUE_SIZE


# -----------------------------------------------------------------------
# Split tracker timeout eviction tests
# -----------------------------------------------------------------------


class TestSplitTrackerEviction:
    def _make_layer(self, mtu=MAXIMUM_MTU, timeout=10.0):
        cc = CongestionController(mtu=mtu)
        return ReliabilityLayer(mtu=mtu, cc=cc, timeout=timeout)

    def test_stale_tracker_evicted(self):
        layer = self._make_layer(timeout=5.0)
        now = 100.0

        # Create a partial split (2 of 3 fragments)
        for i in range(2):
            frame = MessageFrame(
                reliability=Reliability.RELIABLE,
                data=f"part{i}".encode(),
                reliable_message_number=i,
                split_packet_count=3,
                split_packet_id=1,
                split_packet_index=i,
            )
            raw = encode_datagram(i, [frame])
            layer.on_datagram_received(raw, now)

        assert 1 in layer._split_trackers

        # Advance time past timeout and start a new split
        now2 = now + 6.0  # past 5s timeout
        frame_new = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"new_part0",
            reliable_message_number=10,
            split_packet_count=2,
            split_packet_id=2,
            split_packet_index=0,
        )
        raw = encode_datagram(10, [frame_new])
        layer.on_datagram_received(raw, now2)

        # Old tracker should be evicted
        assert 1 not in layer._split_trackers
        assert 2 in layer._split_trackers

    def test_non_stale_tracker_kept(self):
        layer = self._make_layer(timeout=10.0)
        now = 100.0

        # Create a partial split
        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"part0",
            reliable_message_number=0,
            split_packet_count=3,
            split_packet_id=1,
            split_packet_index=0,
        )
        raw = encode_datagram(0, [frame])
        layer.on_datagram_received(raw, now)

        # Advance time but NOT past timeout
        now2 = now + 5.0
        frame_new = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"new_part0",
            reliable_message_number=10,
            split_packet_count=2,
            split_packet_id=2,
            split_packet_index=0,
        )
        raw = encode_datagram(10, [frame_new])
        layer.on_datagram_received(raw, now2)

        # Both trackers should still exist
        assert 1 in layer._split_trackers
        assert 2 in layer._split_trackers

    def test_created_at_is_set(self):
        layer = self._make_layer()
        now = 42.0

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"part0",
            reliable_message_number=0,
            split_packet_count=2,
            split_packet_id=5,
            split_packet_index=0,
        )
        raw = encode_datagram(0, [frame])
        layer.on_datagram_received(raw, now)

        assert layer._split_trackers[5].created_at == 42.0


# -----------------------------------------------------------------------
# Channel validation tests
# -----------------------------------------------------------------------


class TestChannelValidation:
    def _make_layer(self):
        cc = CongestionController(mtu=MAXIMUM_MTU)
        return ReliabilityLayer(mtu=MAXIMUM_MTU, cc=cc)

    def test_valid_channel_zero(self):
        layer = self._make_layer()
        layer.send(b"test", Reliability.RELIABLE_ORDERED, 0)
        assert len(layer._send_queue) == 1

    def test_valid_channel_max(self):
        layer = self._make_layer()
        layer.send(b"test", Reliability.RELIABLE_ORDERED, NUMBER_OF_ORDERED_STREAMS - 1)
        assert len(layer._send_queue) == 1

    def test_invalid_channel_negative(self):
        layer = self._make_layer()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, -1)

    def test_invalid_channel_too_high(self):
        layer = self._make_layer()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, NUMBER_OF_ORDERED_STREAMS)

    def test_invalid_channel_way_too_high(self):
        layer = self._make_layer()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, 100)


# -----------------------------------------------------------------------
# MTU validation tests
# -----------------------------------------------------------------------


class TestMTUValidation:
    def _make_connection(self, **kwargs):
        return Connection(
            address=("127.0.0.1", 19132),
            guid=1234,
            **kwargs,
        )

    def _build_open_reply_1(self, mtu: int) -> bytes:
        """Build a fake ID_OPEN_CONNECTION_REPLY_1 with the given MTU."""
        from aiorak._constants import ID_OPEN_CONNECTION_REPLY_1, OFFLINE_MAGIC

        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(9999)  # server guid
        bs.write_uint8(0)  # has_security = false
        bs.write_uint16(mtu)
        return bs.get_data()

    def _build_open_request_2(self, mtu: int) -> bytes:
        """Build a fake ID_OPEN_CONNECTION_REQUEST_2 with the given MTU."""
        from aiorak._constants import ID_OPEN_CONNECTION_REQUEST_2, OFFLINE_MAGIC

        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_2)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 19132)  # server addr
        bs.write_uint16(mtu)
        bs.write_uint64(5678)  # client guid
        return bs.get_data()

    def test_reply1_valid_mtu(self):
        conn = self._make_connection(is_server=False)
        conn.state = conn.state  # DISCONNECTED
        # Simulate being in CONNECTING state
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(1200)
        result = conn._handle_open_reply_1(data, time.monotonic())
        assert result is not None  # Should produce request 2
        assert conn.mtu == 1200

    def test_reply1_mtu_too_low(self):
        conn = self._make_connection(is_server=False)
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(MINIMUM_MTU - 1)
        result = conn._handle_open_reply_1(data, time.monotonic())
        assert result is None

    def test_reply1_mtu_too_high(self):
        conn = self._make_connection(is_server=False)
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_reply_1(MAXIMUM_MTU + 1)
        result = conn._handle_open_reply_1(data, time.monotonic())
        assert result is None

    def test_reply1_mtu_at_bounds(self):
        for mtu in (MINIMUM_MTU, MAXIMUM_MTU):
            conn = self._make_connection(is_server=False)
            from aiorak._connection import ConnectionState

            conn.state = ConnectionState.CONNECTING
            data = self._build_open_reply_1(mtu)
            result = conn._handle_open_reply_1(data, time.monotonic())
            assert result is not None, f"MTU {mtu} should be accepted"

    def test_request2_valid_mtu(self):
        conn = self._make_connection(is_server=True)
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(1200)
        result = conn._handle_open_request_2(data, time.monotonic())
        assert result is not None
        assert conn.mtu == 1200

    def test_request2_mtu_too_low(self):
        conn = self._make_connection(is_server=True)
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(MINIMUM_MTU - 1)
        result = conn._handle_open_request_2(data, time.monotonic())
        assert result is None

    def test_request2_mtu_too_high(self):
        conn = self._make_connection(is_server=True)
        from aiorak._connection import ConnectionState

        conn.state = ConnectionState.CONNECTING
        data = self._build_open_request_2(MAXIMUM_MTU + 1)
        result = conn._handle_open_request_2(data, time.monotonic())
        assert result is None


# -----------------------------------------------------------------------
# Range list count validation tests
# -----------------------------------------------------------------------


class TestRangeListValidation:
    def test_valid_range_list(self):
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(3)
        for i in range(3):
            bs.write_uint8(1)  # min == max
            bs.write_uint24(i)
        data = bs.get_data()

        bs2 = BitStream(data)
        result = decode_range_list(bs2)
        assert len(result) == 3

    def test_exceeds_max_count(self):
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH + 1)
        data = bs.get_data()

        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="Range list count"):
            decode_range_list(bs2)

    def test_at_max_count(self):
        """Count exactly at the limit should be accepted."""
        bs = BitStream()
        bs.align_write_to_byte()
        bs.write_uint16(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH)
        for i in range(DATAGRAM_MESSAGE_ID_ARRAY_LENGTH):
            bs.write_uint8(1)
            bs.write_uint24(i)
        data = bs.get_data()

        bs2 = BitStream(data)
        result = decode_range_list(bs2)
        assert len(result) == DATAGRAM_MESSAGE_ID_ARRAY_LENGTH


# -----------------------------------------------------------------------
# Client context manager tests
# -----------------------------------------------------------------------


class TestClientContextManager:
    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Client supports async with and calls close() on exit."""
        client = Client(("127.0.0.1", 19132))
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True
            # Don't call original since we're not connected

        client.close = mock_close

        async with client:
            pass

        assert closed

    @pytest.mark.asyncio
    async def test_context_manager_on_exception(self):
        """Client.close() is called even if the body raises."""
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
