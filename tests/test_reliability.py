"""Unit tests for the ReliabilityLayer: send/receive, split/reassembly, ordering."""

import time

from aiorak._congestion import CongestionController
from aiorak._constants import MAX_SPLIT_PACKET_COUNT, MAXIMUM_MTU, SEQ_NUM_MAX
from aiorak._reliability import ReliabilityLayer
from aiorak._types import Reliability
from aiorak._wire import (
    MessageFrame,
    decode_datagram,
    encode_ack,
    encode_datagram,
    encode_nak,
)


def _make_layer(mtu: int = MAXIMUM_MTU) -> ReliabilityLayer:
    cc = CongestionController(mtu=mtu)
    return ReliabilityLayer(mtu=mtu, cc=cc)


class TestSend:
    def test_send_queues_frame(self):
        layer = _make_layer()
        layer.send(b"\x86hello")
        assert len(layer._send_queue) == 1
        assert layer._send_queue[0].data == b"\x86hello"

    def test_update_produces_datagrams(self):
        layer = _make_layer()
        layer.send(b"\x86test")
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1
        # Verify it's a valid data datagram
        header, frames = decode_datagram(datagrams[0])
        assert header.is_data is True
        assert len(frames) >= 1
        assert frames[0].data == b"\x86test"


class TestSplitReassembly:
    def test_split_large_message(self):
        layer = _make_layer(mtu=600)
        # Send a message larger than MTU to force splitting
        large_data = b"\x86" + bytes(range(256)) * 10  # ~2561 bytes
        layer.send(large_data, reliability=Reliability.RELIABLE)
        # All fragments should be queued
        assert len(layer._send_queue) > 1
        # All should have split fields set
        for frame in layer._send_queue:
            assert frame.split_packet_count > 1

    def test_reassemble_split(self):
        layer = _make_layer()
        # Manually create split frames and feed them
        data_parts = [b"part0", b"part1", b"part2"]
        for i, part in enumerate(data_parts):
            frame = MessageFrame(
                reliability=Reliability.RELIABLE,
                data=part,
                reliable_message_number=i,
                split_packet_count=3,
                split_packet_id=42,
                split_packet_index=i,
            )
            # Encode as a datagram and feed to layer
            raw = encode_datagram(i, [frame])
            layer.on_datagram_received(raw, time.monotonic())

        result = layer.poll_receive()
        assert result is not None
        data, channel = result
        assert data == b"part0part1part2"

    def test_reassemble_out_of_order_split(self):
        layer = _make_layer()
        data_parts = [b"aa", b"bb", b"cc"]
        # Feed in reverse order
        for i in reversed(range(3)):
            frame = MessageFrame(
                reliability=Reliability.RELIABLE,
                data=data_parts[i],
                reliable_message_number=i,
                split_packet_count=3,
                split_packet_id=7,
                split_packet_index=i,
            )
            raw = encode_datagram(i, [frame])
            layer.on_datagram_received(raw, time.monotonic())

        result = layer.poll_receive()
        assert result is not None
        data, _ = result
        assert data == b"aabbcc"


class TestOrdering:
    def test_ordering_in_order(self):
        layer = _make_layer()
        for i in range(5):
            frame = MessageFrame(
                reliability=Reliability.RELIABLE_ORDERED,
                data=f"msg{i}".encode(),
                reliable_message_number=i,
                ordering_index=i,
                ordering_channel=0,
            )
            raw = encode_datagram(i, [frame])
            layer.on_datagram_received(raw, time.monotonic())

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])

        assert results == [f"msg{i}".encode() for i in range(5)]

    def test_ordering_out_of_order(self):
        layer = _make_layer()
        # Send indices 2, 0, 1 — only 0 should deliver immediately,
        # then 1 and 2 flush

        # First send index 2 — buffered
        frame2 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg2",
            reliable_message_number=2,
            ordering_index=2,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame2]), time.monotonic())
        assert layer.poll_receive() is None

        # Send index 0 — delivers immediately
        frame0 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg0",
            reliable_message_number=0,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(1, [frame0]), time.monotonic())
        r = layer.poll_receive()
        assert r is not None
        assert r[0] == b"msg0"

        # Send index 1 — delivers 1, then flushes 2
        frame1 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg1",
            reliable_message_number=1,
            ordering_index=1,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(2, [frame1]), time.monotonic())

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        assert results == [b"msg1", b"msg2"]


class TestSequencing:
    def test_sequencing_drops_stale(self):
        layer = _make_layer()
        # Send sequenced index 5 first
        frame5 = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"new",
            sequencing_index=5,
            ordering_index=5,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame5]), time.monotonic())

        # Send sequenced index 3 — should be dropped (stale)
        frame3 = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"old",
            sequencing_index=3,
            ordering_index=3,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(1, [frame3]), time.monotonic())

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        assert results == [b"new"]


class TestAckNak:
    def test_ack_round_trip(self):
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1

        # The datagram should be tracked in history
        assert len(layer._datagram_history) == 1

        # Feed an ACK back for datagram 0
        ack = encode_ack([(0, 0)])
        layer.on_datagram_received(ack, now + 0.01)

        # History should be cleared
        assert len(layer._datagram_history) == 0
        # Resend buffer should be cleared
        assert len(layer._resend_buffer) == 0

    def test_nak_triggers_resend(self):
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)

        # Feed a NAK for datagram 0
        nak = encode_nak([(0, 0)])
        layer.on_datagram_received(nak, now + 0.01)

        # Messages should be marked for immediate resend in resend buffer
        assert any(msg.next_action_time == 0.0 for msg in layer._resend_buffer.values())

    def test_rto_expiry_triggers_resend(self):
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)

        assert len(layer._datagram_history) == 1

        # Advance time past RTO (default is 2.0s when no RTT sample)
        future = now + 3.0
        datagrams = layer.update(future)
        # The expired frame should have been re-sent
        assert len(datagrams) >= 1

    def test_ack_range_wrapping_at_seq_max(self):
        """ACK range spanning SEQ_NUM_MAX boundary is handled correctly."""
        layer = _make_layer()
        now = time.monotonic()

        # Manually place datagram records and resend buffer entries at the wrapping boundary
        lo = SEQ_NUM_MAX - 1
        hi = (SEQ_NUM_MAX + 1) & SEQ_NUM_MAX  # wraps to 0
        for seq in [lo, SEQ_NUM_MAX, hi]:
            frame = MessageFrame(
                reliability=Reliability.RELIABLE,
                data=b"\x86x",
                reliable_message_number=seq,
            )
            from aiorak._reliability import _DatagramRecord, _InFlightMessage

            layer._datagram_history[seq] = _DatagramRecord(
                seq=seq,
                send_time=now,
                reliable_message_numbers=[seq],
                byte_size=10,
            )
            layer._resend_buffer[seq] = _InFlightMessage(
                reliable_message_number=seq,
                frame=frame,
                next_action_time=now + 2.0,
                retransmission_time=2.0,
            )
            layer._unacked_bytes += 10

        # ACK the wrapping range
        ack = encode_ack([(lo, hi)])
        layer.on_datagram_received(ack, now + 0.01)

        # All three should be cleared
        assert len(layer._datagram_history) == 0
        assert len(layer._resend_buffer) == 0


class TestOrderingStale:
    def test_stale_ordering_index_dropped(self):
        """Ordering index behind expected should be silently dropped."""
        layer = _make_layer()
        now = time.monotonic()

        # Deliver message 0 so expected becomes 1
        frame0 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg0",
            reliable_message_number=0,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame0]), now)
        assert layer.poll_receive() is not None

        # Now send a stale message with ordering_index=0 (already delivered)
        stale = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"stale",
            reliable_message_number=1,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(1, [stale]), now)

        # Should not be delivered and should not accumulate in heap
        assert layer.poll_receive() is None
        assert len(layer._ordering_heaps[0]) == 0


class TestMaxSplitCount:
    def test_split_count_exceeding_max_rejected(self):
        """Split packets claiming more than MAX_SPLIT_PACKET_COUNT are rejected."""
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"frag",
            reliable_message_number=0,
            split_packet_count=MAX_SPLIT_PACKET_COUNT + 1,
            split_packet_id=1,
            split_packet_index=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame]), now)

        # Should be rejected — no tracker created
        assert layer.poll_receive() is None
        assert len(layer._split_trackers) == 0


class TestDuplicateDetection:
    """Tests for the new reliable message deduplication."""

    def test_duplicate_reliable_message_rejected(self):
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"hello",
            reliable_message_number=0,
        )
        # Send same message twice in different datagrams
        layer.on_datagram_received(encode_datagram(0, [frame]), now)
        layer.on_datagram_received(encode_datagram(1, [frame]), now)

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        # Should only deliver once
        assert results == [b"hello"]

    def test_duplicate_datagram_rejected(self):
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"world",
            reliable_message_number=0,
        )
        raw = encode_datagram(0, [frame])
        # Send same datagram twice
        layer.on_datagram_received(raw, now)
        layer.on_datagram_received(raw, now)

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        assert results == [b"world"]

    def test_out_of_order_reliable_messages_accepted(self):
        layer = _make_layer()
        now = time.monotonic()

        # Receive reliable messages 2, 0, 1
        for rmn, dg_seq in [(2, 0), (0, 1), (1, 2)]:
            frame = MessageFrame(
                reliability=Reliability.RELIABLE,
                data=f"msg{rmn}".encode(),
                reliable_message_number=rmn,
            )
            layer.on_datagram_received(encode_datagram(dg_seq, [frame]), now)

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        assert len(results) == 3


class TestSplitReliabilityUpgrade:
    """Tests for automatic reliability upgrade of split packets."""

    def test_unreliable_split_upgraded_to_reliable(self):
        layer = _make_layer(mtu=600)
        large_data = b"\x86" + bytes(range(256)) * 10
        layer.send(large_data, reliability=Reliability.UNRELIABLE)
        # All fragments should be RELIABLE (upgraded from UNRELIABLE)
        for frame in layer._send_queue:
            assert frame.reliability == Reliability.RELIABLE

    def test_unreliable_sequenced_split_upgraded(self):
        layer = _make_layer(mtu=600)
        large_data = b"\x86" + bytes(range(256)) * 10
        layer.send(large_data, reliability=Reliability.UNRELIABLE_SEQUENCED)
        for frame in layer._send_queue:
            assert frame.reliability == Reliability.RELIABLE_SEQUENCED


class TestChannelPropagation:
    """Tests that ordered/sequenced messages propagate ordering_channel."""

    def test_sequenced_preserves_channel(self):
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"test",
            sequencing_index=0,
            ordering_index=0,
            ordering_channel=5,
        )
        layer.on_datagram_received(encode_datagram(0, [frame]), now)

        result = layer.poll_receive()
        assert result is not None
        _, channel = result
        assert channel == 5

    def test_ordered_preserves_channel(self):
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"test",
            reliable_message_number=0,
            ordering_index=0,
            ordering_channel=3,
        )
        layer.on_datagram_received(encode_datagram(0, [frame]), now)

        result = layer.poll_receive()
        assert result is not None
        _, channel = result
        assert channel == 3


class TestWraparound:
    """Tests for 24-bit counter wraparound."""

    def test_ordering_counter_wraps(self):
        layer = _make_layer()
        layer._next_ordering_index[0] = SEQ_NUM_MAX
        layer.send(b"test", Reliability.RELIABLE_ORDERED, 0)
        # After sending, the counter should have wrapped to 0
        assert layer._next_ordering_index[0] == 0

    def test_sequencing_counter_wraps(self):
        layer = _make_layer()
        layer._next_sequencing_index[0] = SEQ_NUM_MAX
        layer.send(b"test", Reliability.UNRELIABLE_SEQUENCED, 0)
        assert layer._next_sequencing_index[0] == 0
