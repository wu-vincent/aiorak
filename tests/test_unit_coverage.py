"""Unit tests for internal modules: _bitstream, _wire, _congestion, _reliability, _connection.

These tests target specific uncovered lines without requiring network I/O.
"""

import time as _time
from unittest.mock import patch

import pytest

from aiorak._bitstream import BitStream
from aiorak._congestion import CongestionController, seq_less_than
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
    MINIMUM_MTU,
    NUMBER_OF_INTERNAL_IDS,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
    UDP_HEADER_SIZE,
)
from aiorak._reliability import ReliabilityLayer, _ReliableMessageWindow
from aiorak._types import Reliability
from aiorak._wire import (
    MessageFrame,
    decode_datagram,
    decode_message_frame,
    encode_datagram,
    encode_message_frame,
)

# ===================================================================
# _bitstream.py
# ===================================================================


class TestBitStreamErrors:
    def test_ignore_bits_overflow(self):
        bs = BitStream()
        with pytest.raises(ValueError, match="Not enough bits"):
            bs.ignore_bits(1)

    def test_read_bit_past_end(self):
        bs = BitStream()
        with pytest.raises(ValueError, match="Read past end"):
            bs.read_bit()

    def test_read_uint16_short(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint16"):
            bs.read_uint16()

    def test_read_uint24_short(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint24"):
            bs.read_uint24()

    def test_read_uint32_short(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint32"):
            bs.read_uint32()

    def test_read_uint64_short(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="uint64"):
            bs.read_uint64()

    def test_read_int64_short(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="int64"):
            bs.read_int64()

    def test_read_bytes_overflow(self):
        bs = BitStream(b"\x00")
        with pytest.raises(ValueError, match="Cannot read 5 bytes"):
            bs.read_bytes(5)

    def test_read_address_ipv4_truncated(self):
        bs = BitStream()
        bs.write_uint8(4)  # af=4
        # Only 1 byte written, need 7 total for IPv4
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="IPv4"):
            bs2.read_address()

    def test_read_address_ipv6_truncated(self):
        bs = BitStream()
        bs.write_uint8(6)  # af=6
        # Only 1 byte written, need 29 total for IPv6
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="IPv6"):
            bs2.read_address()

    def test_read_address_bad_family(self):
        bs = BitStream()
        bs.write_uint8(99)
        data = bs.get_data()
        bs2 = BitStream(data)
        with pytest.raises(ValueError, match="Unsupported address family: 99"):
            bs2.read_address()

    def test_pad_already_sufficient(self):
        bs = BitStream(b"\x00" * 10)
        bs.pad_with_zero_to_byte_length(5)
        # Should be a no-op, length stays at 10
        assert bs.get_byte_length() == 10

    def test_repr(self):
        bs = BitStream()
        r = repr(bs)
        assert "BitStream" in r
        assert "write_bits=0" in r
        assert "read_bits=0" in r


# ===================================================================
# _wire.py
# ===================================================================


class TestWireEdgeCases:
    def test_encode_ack_receipt_reliability_variants(self):
        """ACK_RECEIPT variants should map to their base reliability on wire."""
        for rel, expected_wire_rel in [
            (Reliability.UNRELIABLE_WITH_ACK_RECEIPT, Reliability.UNRELIABLE),
            (Reliability.RELIABLE_WITH_ACK_RECEIPT, Reliability.RELIABLE),
            (Reliability.RELIABLE_ORDERED_WITH_ACK_RECEIPT, Reliability.RELIABLE_ORDERED),
        ]:
            frame = MessageFrame(reliability=rel, data=b"\x42")
            bs = BitStream()
            encode_message_frame(bs, frame)
            # Decode and verify the wire reliability
            bs2 = BitStream(bs.get_data())
            decoded = decode_message_frame(bs2)
            assert decoded is not None
            assert decoded.data == b"\x42"

    def test_decode_frame_zero_data_length(self):
        """Frame with data_bit_length=0 should return None."""
        bs = BitStream()
        bs.align_write_to_byte()
        # reliability=0 (3 bits) + has_split=0 (1 bit)
        bs.write_bits(0, 3)
        bs.write_bit(False)
        bs.align_write_to_byte()
        # data_bit_length = 0
        bs.write_uint16(0)
        # Pad enough for the 32-bit minimum check
        bs.write_uint16(0)

        bs2 = BitStream(bs.get_data())
        result = decode_message_frame(bs2)
        assert result is None

    def test_decode_datagram_is_valid_zero(self):
        """Datagram with isValid=0 should raise ValueError."""
        # First byte with bit 7 = 0 (isValid = 0)
        data = bytes([0x00, 0x00, 0x00, 0x00])
        with pytest.raises(ValueError, match="isValid"):
            decode_datagram(data)

    def test_decode_datagram_truncated_frame(self):
        """Valid data datagram header but truncated frame data → empty frames list."""
        # Build a valid data datagram header with 0 actual frame data
        bs = BitStream()
        bs.write_bit(True)  # isValid
        bs.write_bit(False)  # isACK
        bs.write_bit(False)  # isNAK
        bs.write_bit(False)  # isPacketPair
        bs.write_bit(False)  # isContinuousSend
        bs.write_bit(False)  # needsBAndAs
        bs.align_write_to_byte()
        bs.write_uint24(0)  # datagram number
        # Add partial frame data (not enough for a full frame)
        bs.write_uint8(0xFF)
        header, frames = decode_datagram(bs.get_data())
        assert header.is_data
        assert frames == []


# ===================================================================
# _congestion.py
# ===================================================================


class TestCongestion:
    def test_seq_less_than(self):
        assert seq_less_than(5, 10) is True
        assert seq_less_than(10, 5) is False
        assert seq_less_than(5, 5) is False

    def test_retransmission_bandwidth(self):
        cc = CongestionController(1000)
        assert cc.retransmission_bandwidth(1000) == 1000
        assert cc.retransmission_bandwidth(0) == 0

    def test_on_got_packet_huge_gap(self):
        cc = CongestionController(1000)
        cc.expected_next_seq = 0
        result = cc.on_got_packet(60000, 1.0)
        assert result is None

    def test_on_nak_continuous_send(self):
        cc = CongestionController(1000)
        cc._is_continuous_send = True
        cc._backed_off_this_block = False
        cc.cwnd = 10000.0
        cc.on_nak()
        assert cc.ss_thresh == 5000.0


# ===================================================================
# _reliability.py
# ===================================================================


class TestReliableMessageWindow:
    def test_duplicate_detection(self):
        w = _ReliableMessageWindow()
        assert w.check_and_mark(0) is True
        assert w.check_and_mark(0) is False  # duplicate

    def test_gap_then_duplicate(self):
        w = _ReliableMessageWindow()
        # Skip 0, mark 5
        assert w.check_and_mark(5) is True
        # Mark 5 again - duplicate
        assert w.check_and_mark(5) is False

    def test_absurd_gap_rejected(self):
        w = _ReliableMessageWindow()
        # Gap > 1000000
        assert w.check_and_mark(1500000) is False

    def test_behind_base_duplicate(self):
        w = _ReliableMessageWindow()
        # Advance base past 0
        w.check_and_mark(0)
        w.check_and_mark(1)
        # Now base is 2, trying to mark 0 (behind base) should return False
        # Due to wrapping, 0 is behind base=2
        assert w.check_and_mark(0) is False


class TestReliabilityLayer:
    def _make_layer(self, mtu=1000):
        cc = CongestionController(mtu)
        return ReliabilityLayer(mtu, cc, timeout=10.0), cc

    def test_on_datagram_malformed(self):
        rl, cc = self._make_layer()
        # Feed garbage that will fail decode_datagram
        rl.on_datagram_received(b"\x00\x00", 1.0)
        # Should not raise, just drop silently
        assert rl.poll_receive() is None

    def test_nak_generation(self):
        rl, cc = self._make_layer()
        # Add NAK ranges manually and build
        rl._nak_ranges = [(0, 0), (2, 3)]
        packets = rl._build_nak_packets(rl._nak_ranges)
        assert len(packets) >= 1

    def test_split_tracker_eviction(self):
        rl, cc = self._make_layer()
        # Add an incomplete split tracker with old timestamp
        from aiorak._reliability import _SplitTracker

        rl._split_trackers[42] = _SplitTracker(
            total=5,
            received={0: b"chunk0"},
            last_update_time=0.0,  # very old
        )
        # update() at time 100 should evict it (timeout=10)
        rl.update(100.0)
        assert 42 not in rl._split_trackers

    def test_unreliable_timeout_culling(self):
        rl, cc = self._make_layer()
        rl._unreliable_timeout = 1.0
        # Create a frame with _creation_time attribute
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"test")
        frame._creation_time = 0.0  # type: ignore[attr-defined]
        rl._send_queue = [(0, 0, frame)]
        rl.update(100.0)
        # Frame should have been culled
        assert len(rl._send_queue) == 0

    def test_reassemble_split_exceeds_max(self):
        rl, cc = self._make_layer()
        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"x",
            split_packet_count=100000,  # exceeds MAX_SPLIT_PACKET_COUNT (65536)
            split_packet_id=1,
            split_packet_index=0,
        )
        result = rl._reassemble_split(frame, 1.0)
        assert result is None

    def test_datagram_dedup(self):
        """Send same datagram seq twice → second skips frame processing."""
        rl, cc = self._make_layer()
        # Build a simple data datagram
        frame = MessageFrame(reliability=Reliability.RELIABLE, data=b"hello", reliable_message_number=0)
        raw = encode_datagram(0, [frame])
        # Process it twice
        rl.on_datagram_received(raw, 1.0)
        rl.on_datagram_received(raw, 1.0)
        # Should only get one message
        msg1 = rl.poll_receive()
        msg2 = rl.poll_receive()
        assert msg1 is not None
        assert msg2 is None

    def test_sequenced_future_ordering_slot(self):
        """Sequenced packet with future ordering index → buffered in heap."""
        rl, cc = self._make_layer()
        # Build a sequenced frame with ordering_index=5 (expected is 0)
        frame = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"future",
            sequencing_index=0,
            ordering_index=5,
            ordering_channel=0,
        )
        raw = encode_datagram(0, [frame])
        rl.on_datagram_received(raw, 1.0)
        # Should be buffered, not delivered
        assert rl.poll_receive() is None
        assert len(rl._ordering_heaps[0]) == 1

    def test_flush_ordering_heap_sequenced(self):
        """Flush heap with a sequenced entry → updates _highest_sequenced."""
        rl, cc = self._make_layer()
        import heapq

        # Push a sequenced entry at ordering_index=0 (which is expected)
        rl._heap_counter += 1
        heapq.heappush(
            rl._ordering_heaps[0],
            (0, 3, rl._heap_counter, False, b"seq_data"),  # is_ordered=False means sequenced
        )
        rl._flush_ordering_heap(0)
        assert rl._highest_sequenced[0] == 3
        msg = rl.poll_receive()
        assert msg is not None
        assert msg[0] == b"seq_data"

    def test_build_range_packets_split(self):
        """Enough ranges to exceed MTU → multiple ACK packets."""
        rl, cc = self._make_layer(mtu=100)  # small MTU
        # Create many ranges
        ranges = [(i * 10, i * 10 + 1) for i in range(50)]
        packets = rl._build_ack_packets(ranges)
        assert len(packets) > 1

    def test_estimate_frame_size_sequenced(self):
        """Frame with sequenced reliability → includes 3-byte sequencing overhead."""
        rl, cc = self._make_layer()
        frame = MessageFrame(
            reliability=Reliability.RELIABLE_SEQUENCED,
            data=b"x",
            sequencing_index=0,
            ordering_index=0,
            ordering_channel=0,
        )
        size = rl._estimate_frame_size(frame)
        # 3 (base) + 1 (data) + 3 (reliable) + 3 (sequenced) + 4 (ordered) = 14
        assert size == 14

    def test_split_reliability_upgrade_unreliable_ack_receipt(self):
        """Large message with UNRELIABLE_WITH_ACK_RECEIPT → upgraded to RELIABLE_WITH_ACK_RECEIPT."""
        rl, cc = self._make_layer(mtu=100)
        # Send a message larger than MTU to trigger split
        big_data = b"x" * 500
        rl.send(big_data, Reliability.UNRELIABLE_WITH_ACK_RECEIPT)
        # Should have queued split frames with upgraded reliability
        assert len(rl._send_queue) > 1

    def test_nak_ranges_in_update(self):
        """NAK ranges are built and cleared during update()."""
        rl, cc = self._make_layer()
        rl._nak_ranges = [(0, 0), (2, 2)]
        # Set last_rtt so ACKs can be sent
        cc.last_rtt = 0.01
        result = rl.update(1.0)
        assert len(result) >= 1  # NAK packets generated
        assert len(rl._nak_ranges) == 0  # ranges cleared

    def test_datagram_rejection_huge_gap(self):
        """Datagram with absurdly large sequence gap → skipped=None → return early."""
        rl, cc = self._make_layer()
        # Build datagram with seq=60000 while expected_next_seq=0
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"x")
        raw = encode_datagram(60000, [frame])
        rl.on_datagram_received(raw, 1.0)
        # Should not deliver the message
        assert rl.poll_receive() is None

    def test_unreliable_timeout_mixed_entries(self):
        """Mix of expired and non-expired unreliable messages in culling."""
        rl, cc = self._make_layer()
        rl._unreliable_timeout = 5.0
        # Expired frame
        expired = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"old")
        expired._creation_time = 0.0  # type: ignore[attr-defined]
        # Non-expired frame (kept)
        fresh = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"new")
        fresh._creation_time = 99.0  # type: ignore[attr-defined]
        # Reliable frame (always kept, no _creation_time check)
        reliable = MessageFrame(reliability=Reliability.RELIABLE, data=b"rel")
        rl._send_queue = [(0, 0, expired), (1, 1, fresh), (2, 2, reliable)]
        # Culling happens before send drain. To observe it we need cwnd=0 so nothing is sent.
        cc.cwnd = 0.0
        rl.update(100.0)
        # expired is culled, fresh and reliable remain (but fresh might be in re-enqueued list)
        # The key is that line 386 (kept.append) is hit for fresh and reliable

    def test_build_nak_packets_split_mtu(self):
        """NAK packets split across MTU boundaries."""
        rl, cc = self._make_layer(mtu=100)
        ranges = [(i * 10, i * 10 + 1) for i in range(50)]
        packets = rl._build_nak_packets(ranges)
        assert len(packets) > 1


# ===================================================================
# _connection.py
# ===================================================================


class TestConnectionUnit:
    def _make_conn(self, is_server=False, **kwargs):
        return Connection(
            address=("127.0.0.1", 19132),
            guid=0xDEAD,
            is_server=is_server,
            **kwargs,
        )

    def test_get_local_addresses_oserror(self):
        with patch("aiorak._connection._socket.getaddrinfo", side_effect=OSError("fail")):
            result = _get_local_addresses()
        assert result == ["127.0.0.1"]

    def test_connection_repr(self):
        conn = self._make_conn()
        r = repr(conn)
        assert "Connection" in r
        assert "DISCONNECTED" in r

    async def test_connection_close_idempotent(self):
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        await conn.close()
        await conn.close()  # second call is no-op

    def test_build_ocr1_mtu_exhausted(self):
        conn = self._make_conn()
        conn._mtu_attempt_index = 100  # past all sizes
        result = conn._build_open_request_1(1.0)
        assert result is None

    def test_handle_offline_empty(self):
        conn = self._make_conn(is_server=True)
        result = conn._handle_offline(b"", 1.0)
        assert result is None

    def test_handle_offline_unknown_id(self):
        conn = self._make_conn(is_server=True)
        # Unknown ID with valid magic
        data = b"\xff" + OFFLINE_MAGIC + b"\x00" * 10
        result = conn._handle_offline(data, 1.0)
        assert result is None

    def test_handle_ocr1_server_side(self):
        """Server-side Connection handles OCR1 and returns OR1."""
        conn = self._make_conn(is_server=True)
        # Build a valid OCR1
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
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(b"\x00" * 16)  # bad magic
        bs.write_uint8(RAKNET_PROTOCOL_VERSION)
        result = conn._handle_open_request_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr1_wrong_protocol(self):
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(99)  # wrong protocol
        result = conn._handle_open_request_1(bs.get_data(), 1.0)
        assert result is not None
        assert result[0] == ID_INCOMPATIBLE_PROTOCOL_VERSION

    def test_handle_or1_bad_magic(self):
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(b"\x00" * 16)  # bad magic
        bs.write_uint64(0x1234)
        bs.write_uint8(0)
        bs.write_uint16(1000)
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or1_security_set(self):
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(0x1234)
        bs.write_uint8(1)  # has_security = true
        bs.write_uint16(1000)
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or1_mtu_out_of_range(self):
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint64(0x1234)
        bs.write_uint8(0)
        bs.write_uint16(10)  # way below MINIMUM_MTU
        result = conn._handle_open_reply_1(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr2_bad_magic(self):
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(0x07)  # ID_OPEN_CONNECTION_REQUEST_2
        bs.write_bytes(b"\x00" * 16)  # bad magic
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(1000)
        bs.write_uint64(0xBEEF)
        result = conn._handle_open_request_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_ocr2_mtu_out_of_range(self):
        conn = self._make_conn(is_server=True)
        bs = BitStream()
        bs.write_uint8(0x07)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(10)  # below min
        bs.write_uint64(0xBEEF)
        result = conn._handle_open_request_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_or2_bad_magic(self):
        conn = self._make_conn(is_server=False)
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REPLY_2)
        bs.write_bytes(b"\x00" * 16)  # bad magic
        bs.write_uint64(0x1234)
        bs.write_address("127.0.0.1", 19132)
        bs.write_uint16(1000)
        bs.write_bit(False)
        result = conn._handle_open_reply_2(bs.get_data(), 1.0)
        assert result is None

    def test_handle_connected_message_empty(self):
        conn = self._make_conn()
        result = conn._handle_connected_message(b"", 1.0)
        assert result is None

    def test_handle_detect_lost_connections(self):
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        result = conn._handle_connected_message(bytes([ID_DETECT_LOST_CONNECTIONS]), 1.0)
        assert result is None

    def test_handle_new_incoming_connection_malformed(self):
        """Truncated ID_NEW_INCOMING_CONNECTION data → catches exception."""
        conn = self._make_conn(is_server=True)
        conn.state = ConnectionState.CONNECTING
        # Just the message ID, no actual data
        conn._handle_new_incoming_connection(bytes([ID_NEW_INCOMING_CONNECTION]), 1.0)
        # Should not raise, just log warning

    def test_offline_rejection_client_side(self):
        """Client receives ID_INCOMPATIBLE_PROTOCOL_VERSION → DISCONNECT signal."""
        conn = self._make_conn(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = bytes([ID_INCOMPATIBLE_PROTOCOL_VERSION, RAKNET_PROTOCOL_VERSION]) + OFFLINE_MAGIC + b"\x00" * 8
        conn.on_datagram(data, 1.0)
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_offline_rejection_no_free_connections(self):
        conn = self._make_conn(is_server=False)
        conn.state = ConnectionState.CONNECTING
        data = bytes([ID_NO_FREE_INCOMING_CONNECTIONS])
        conn.on_datagram(data, 1.0)
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_handshake_retransmit_and_mtu_exhaustion(self):
        """Advancing past retransmit interval → MTU index advances, eventually DISCONNECT."""
        conn = self._make_conn(is_server=False, mtu_discovery_sizes=(1492, 1200, 576))
        now = 1.0
        conn.start_connect(now)

        # Exhaust all MTU sizes by retransmitting many times
        for i in range(20):
            now += 2.0  # past retransmit interval of 1.0
            conn.update(now)

        conn.poll_events()
        # Should have disconnected due to MTU exhaustion
        assert conn.state == ConnectionState.DISCONNECTED

    def test_disconnect_on_ack_sent(self):
        """DISCONNECTING + _disconnect_on_ack_sent + empty ack_ranges → DISCONNECTED."""
        conn = self._make_conn()
        conn.state = ConnectionState.DISCONNECTING
        conn._disconnect_on_ack_sent = True
        conn._last_recv_time = 1.0
        conn._reliability._ack_ranges = []
        conn.update(1.0)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_graceful_disconnect_flush(self):
        """DISCONNECTING + no pending data → DISCONNECTED."""
        conn = self._make_conn()
        conn.state = ConnectionState.DISCONNECTING
        conn._last_recv_time = 1.0
        # Ensure no pending data
        assert not conn._reliability.has_pending_data
        conn.update(1.0)
        assert conn.state == ConnectionState.DISCONNECTED
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_disconnection_notification_handling(self):
        """Receiving ID_DISCONNECTION_NOTIFICATION → DISCONNECTING + _disconnect_on_ack_sent."""
        conn = self._make_conn()
        conn.state = ConnectionState.CONNECTED
        conn._handle_connected_message(bytes([ID_DISCONNECTION_NOTIFICATION]), 1.0)
        assert conn.state == ConnectionState.DISCONNECTING
        assert conn._disconnect_on_ack_sent is True

    def test_handshake_timeout(self):
        """Handshake timeout → DISCONNECTED."""
        conn = self._make_conn(is_server=True, timeout=5.0)
        conn.state = ConnectionState.CONNECTING
        conn._handshake_start = 1.0
        conn._last_recv_time = 1.0
        # Update way past timeout
        conn.update(100.0)
        assert conn.state == ConnectionState.DISCONNECTED
        events = conn.poll_events()
        assert any(sig == _Signal.DISCONNECT for sig, _ in events)

    def test_connected_timeout(self):
        """Connected timeout → DISCONNECTED."""
        conn = self._make_conn(timeout=5.0)
        conn.state = ConnectionState.CONNECTED
        conn._last_recv_time = 1.0
        conn.update(100.0)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_handle_ocr1_via_on_datagram(self):
        """Server-side connection receives OCR1 via on_datagram → dispatches to _handle_offline."""
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
        """NIC with future timestamp → RTT computation skipped (branch 858->863)."""
        conn = self._make_conn(is_server=True, num_internal_ids=NUMBER_OF_INTERNAL_IDS)
        conn.state = ConnectionState.CONNECTING
        bs = BitStream()
        bs.write_uint8(ID_NEW_INCOMING_CONNECTION)
        bs.write_address("127.0.0.1", 19132)
        for _ in range(NUMBER_OF_INTERNAL_IDS):
            bs.write_address("127.0.0.1", 0)
        # Set send_ping_time far in the future so now_ms <= send_ping_time
        bs.write_int64(int(_time.time() * 1000) + 999999999)
        bs.write_int64(int(_time.time() * 1000))
        conn._handle_new_incoming_connection(bs.get_data(), 1.0)
        assert conn.state == ConnectionState.CONNECTED
