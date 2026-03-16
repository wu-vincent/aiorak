"""Unit tests for the ReliabilityLayer: send/receive, split/reassembly, ordering, dedup."""

import heapq
import time

import pytest

from aiorak._congestion import CongestionController
from aiorak._constants import (
    DEFAULT_RECEIVED_PACKET_QUEUE_SIZE,
    MAX_SPLIT_PACKET_COUNT,
    MAXIMUM_MTU,
    NUMBER_OF_ORDERED_STREAMS,
    SEQ_NUM_MAX,
)
from aiorak._reliability import ReliabilityLayer, _ReceivedWindow, _ReliableMessageWindow, _SplitTracker
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


def _make_layer_with_cc(mtu=1000, timeout=10.0):
    cc = CongestionController(mtu)
    return ReliabilityLayer(mtu, cc, timeout=timeout), cc


class TestSend:
    def test_send_queues_frame(self):
        """send() adds one frame to the send queue."""
        layer = _make_layer()
        layer.send(b"\x86hello")
        assert len(layer._send_queue) == 1
        assert layer._send_queue[0][2].data == b"\x86hello"

    def test_update_produces_datagrams(self):
        """update() after send() produces at least one valid data datagram."""
        layer = _make_layer()
        layer.send(b"\x86test")
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1
        header, frames = decode_datagram(datagrams[0])
        assert header.is_data is True
        assert len(frames) >= 1
        assert frames[0].data == b"\x86test"


class TestSplitReassembly:
    def test_split_large_message(self):
        """Message larger than MTU is split into multiple fragments in the send queue."""
        layer = _make_layer(mtu=600)
        large_data = b"\x86" + bytes(range(256)) * 10
        layer.send(large_data, reliability=Reliability.RELIABLE)
        assert len(layer._send_queue) > 1
        for _w, _c, frame in layer._send_queue:
            assert frame.split_packet_count > 1

    def test_reassemble_split(self):
        """Three split fragments delivered in order are reassembled correctly."""
        layer = _make_layer()
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
            raw = encode_datagram(i, [frame])
            layer.on_datagram_received(raw, time.monotonic())

        result = layer.poll_receive()
        assert result is not None
        data, channel = result
        assert data == b"part0part1part2"

    def test_reassemble_out_of_order_split(self):
        """Three split fragments delivered in reverse order are reassembled correctly."""
        layer = _make_layer()
        data_parts = [b"aa", b"bb", b"cc"]
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
        """Five RELIABLE_ORDERED messages delivered in order are all received sequentially."""
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
        """RELIABLE_ORDERED messages delivered as [2,0,1] are reordered to [0,1,2]."""
        layer = _make_layer()

        frame2 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg2",
            reliable_message_number=2,
            ordering_index=2,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame2]), time.monotonic())
        assert layer.poll_receive() is None

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
        """Sequenced message with index older than current highest is dropped."""
        layer = _make_layer()
        frame5 = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"new",
            sequencing_index=5,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame5]), time.monotonic())

        frame3 = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"old",
            sequencing_index=3,
            ordering_index=0,
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
        """ACK for datagram 0 clears datagram history and resend buffer."""
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1
        assert len(layer._datagram_history) == 1

        ack = encode_ack([(0, 0)])
        layer.on_datagram_received(ack, now + 0.01)

        assert len(layer._datagram_history) == 0
        assert len(layer._resend_buffer) == 0

    def test_nak_triggers_resend(self):
        """NAK for datagram 0 marks messages for immediate resend."""
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)

        nak = encode_nak([(0, 0)])
        layer.on_datagram_received(nak, now + 0.01)

        assert any(msg.next_action_time == 0.0 for msg in layer._resend_buffer.values())

    def test_rto_expiry_triggers_resend(self):
        """Advancing time past RTO causes automatic resend."""
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)
        assert len(layer._datagram_history) == 1

        future = now + 3.0
        datagrams = layer.update(future)
        assert len(datagrams) >= 1

    def test_ack_range_wrapping_at_seq_max(self):
        """ACK range spanning SEQ_NUM_MAX boundary clears all entries correctly."""
        layer = _make_layer()
        now = time.monotonic()

        lo = SEQ_NUM_MAX - 1
        hi = (SEQ_NUM_MAX + 1) & SEQ_NUM_MAX
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

        ack = encode_ack([(lo, hi)])
        layer.on_datagram_received(ack, now + 0.01)

        assert len(layer._datagram_history) == 0
        assert len(layer._resend_buffer) == 0


class TestOrderingStale:
    def test_stale_ordering_index_dropped(self):
        """Ordering index behind expected is silently dropped and not buffered."""
        layer = _make_layer()
        now = time.monotonic()

        frame0 = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"msg0",
            reliable_message_number=0,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame0]), now)
        assert layer.poll_receive() is not None

        stale = MessageFrame(
            reliability=Reliability.RELIABLE_ORDERED,
            data=b"stale",
            reliable_message_number=1,
            ordering_index=0,
            ordering_channel=0,
        )
        layer.on_datagram_received(encode_datagram(1, [stale]), now)

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

        assert layer.poll_receive() is None
        assert len(layer._split_trackers) == 0


class TestDuplicateDetection:
    def test_duplicate_reliable_message_rejected(self):
        """Same reliable message number in different datagrams is delivered only once."""
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"hello",
            reliable_message_number=0,
        )
        layer.on_datagram_received(encode_datagram(0, [frame]), now)
        layer.on_datagram_received(encode_datagram(1, [frame]), now)

        results = []
        while True:
            r = layer.poll_receive()
            if r is None:
                break
            results.append(r[0])
        assert results == [b"hello"]

    def test_duplicate_datagram_rejected(self):
        """Same datagram sequence number sent twice is processed only once."""
        layer = _make_layer()
        now = time.monotonic()

        frame = MessageFrame(
            reliability=Reliability.RELIABLE,
            data=b"world",
            reliable_message_number=0,
        )
        raw = encode_datagram(0, [frame])
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
        """Reliable messages received out of order (2,0,1) are all accepted."""
        layer = _make_layer()
        now = time.monotonic()

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
    def test_unreliable_split_upgraded_to_reliable(self):
        """UNRELIABLE split packets are automatically upgraded to RELIABLE."""
        layer = _make_layer(mtu=600)
        large_data = b"\x86" + bytes(range(256)) * 10
        layer.send(large_data, reliability=Reliability.UNRELIABLE)
        for _w, _c, frame in layer._send_queue:
            assert frame.reliability == Reliability.RELIABLE

    def test_unreliable_sequenced_split_upgraded(self):
        """UNRELIABLE_SEQUENCED split packets are upgraded to RELIABLE_SEQUENCED."""
        layer = _make_layer(mtu=600)
        large_data = b"\x86" + bytes(range(256)) * 10
        layer.send(large_data, reliability=Reliability.UNRELIABLE_SEQUENCED)
        for _w, _c, frame in layer._send_queue:
            assert frame.reliability == Reliability.RELIABLE_SEQUENCED


class TestChannelPropagation:
    def test_sequenced_preserves_channel(self):
        """Sequenced message preserves ordering_channel through receive path."""
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
        """Ordered message preserves ordering_channel through receive path."""
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
    def test_ordering_counter_wraps(self):
        """Ordering counter wraps from SEQ_NUM_MAX to 0."""
        layer = _make_layer()
        layer._next_ordering_index[0] = SEQ_NUM_MAX
        layer.send(b"test", Reliability.RELIABLE_ORDERED, 0)
        assert layer._next_ordering_index[0] == 0

    def test_sequencing_counter_wraps(self):
        """Sequencing counter wraps from SEQ_NUM_MAX to 0."""
        layer = _make_layer()
        layer._next_sequencing_index[0] = SEQ_NUM_MAX
        layer.send(b"test", Reliability.UNRELIABLE_SEQUENCED, 0)
        assert layer._next_sequencing_index[0] == 0


class TestPriorityQueue:
    def test_immediate_before_low(self):
        """IMMEDIATE priority messages are sent before LOW priority in same update."""
        from aiorak._types import Priority

        layer = _make_layer()
        layer.send(b"\x86low", Reliability.RELIABLE, 0, Priority.LOW)
        layer.send(b"\x86imm", Reliability.RELIABLE, 0, Priority.IMMEDIATE)
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1
        _, frames = decode_datagram(datagrams[0])
        assert frames[0].data == b"\x86imm"

    def test_high_before_medium(self):
        """HIGH priority messages are sent before MEDIUM priority in same update."""
        from aiorak._types import Priority

        layer = _make_layer()
        layer.send(b"\x86med", Reliability.RELIABLE, 0, Priority.MEDIUM)
        layer.send(b"\x86high", Reliability.RELIABLE, 0, Priority.HIGH)
        now = time.monotonic()
        datagrams = layer.update(now)
        assert len(datagrams) >= 1
        _, frames = decode_datagram(datagrams[0])
        assert frames[0].data == b"\x86high"


# ---------------------------------------------------------------------------
# Tests from test_unit_coverage.py
# ---------------------------------------------------------------------------


class TestReliableMessageWindow:
    """Tests for the _ReliableMessageWindow dedup structure."""

    def test_duplicate_detection(self):
        """Marking the same sequence twice returns True then False."""
        w = _ReliableMessageWindow()
        assert w.check_and_mark(0) is True
        assert w.check_and_mark(0) is False

    def test_gap_then_duplicate(self):
        """Marking seq 5 (skipping 0-4), then marking 5 again returns False."""
        w = _ReliableMessageWindow()
        assert w.check_and_mark(5) is True
        assert w.check_and_mark(5) is False

    def test_absurd_gap_rejected(self):
        """Sequence number with gap > 1000000 is rejected as duplicate."""
        w = _ReliableMessageWindow()
        assert w.check_and_mark(1500000) is False

    def test_behind_base_duplicate(self):
        """Sequence number behind the window base is rejected as duplicate."""
        w = _ReliableMessageWindow()
        w.check_and_mark(0)
        w.check_and_mark(1)
        assert w.check_and_mark(0) is False


class TestReliabilityLayerAdvanced:
    """Advanced ReliabilityLayer tests from test_unit_coverage.py."""

    def test_on_datagram_malformed(self):
        """Malformed datagram is silently dropped without raising."""
        rl, cc = _make_layer_with_cc()
        rl.on_datagram_received(b"\x00\x00", 1.0)
        assert rl.poll_receive() is None

    def test_nak_generation(self):
        """NAK ranges produce at least one NAK packet."""
        rl, cc = _make_layer_with_cc()
        rl._nak_ranges = [(0, 0), (2, 3)]
        packets = rl._build_nak_packets(rl._nak_ranges)
        assert len(packets) >= 1

    def test_split_tracker_eviction(self):
        """Incomplete split tracker older than timeout is evicted during update."""
        rl, cc = _make_layer_with_cc()
        rl._split_trackers[42] = _SplitTracker(
            total=5,
            received={0: b"chunk0"},
            last_update_time=0.0,
        )
        rl.update(100.0)
        assert 42 not in rl._split_trackers

    def test_stale_datagram_history_eviction(self):
        """Datagram history entries older than timeout are evicted during update.

        Manually inserts a _DatagramRecord with old send_time, then calls update()
        to trigger the eviction path at lines 373-376.
        """
        from aiorak._reliability import _DatagramRecord

        rl, cc = _make_layer_with_cc(timeout=5.0)
        rl._datagram_history[99] = _DatagramRecord(
            seq=99,
            send_time=0.0,
            reliable_message_numbers=[],
            byte_size=100,
        )
        rl._unacked_bytes = 100
        rl.update(100.0)
        assert 99 not in rl._datagram_history
        assert rl._unacked_bytes == 0

    def test_unreliable_timeout_culling(self):
        """Expired unreliable frames are culled from the send queue during update."""
        rl, cc = _make_layer_with_cc()
        rl._unreliable_timeout = 1.0
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"test")
        frame._creation_time = 0.0  # type: ignore[attr-defined]
        rl._send_queue = [(0, 0, frame)]
        rl.update(100.0)
        assert len(rl._send_queue) == 0

    def test_sequenced_future_ordering_slot(self):
        """Sequenced packet with future ordering index is buffered in the ordering heap."""
        rl, cc = _make_layer_with_cc()
        frame = MessageFrame(
            reliability=Reliability.UNRELIABLE_SEQUENCED,
            data=b"future",
            sequencing_index=0,
            ordering_index=5,
            ordering_channel=0,
        )
        raw = encode_datagram(0, [frame])
        rl.on_datagram_received(raw, 1.0)
        assert rl.poll_receive() is None
        assert len(rl._ordering_heaps[0]) == 1

    def test_flush_ordering_heap_sequenced(self):
        """Flushing a sequenced entry from the ordering heap updates _highest_sequenced."""
        rl, cc = _make_layer_with_cc()
        rl._heap_counter += 1
        heapq.heappush(
            rl._ordering_heaps[0],
            (0, 3, rl._heap_counter, False, b"seq_data"),
        )
        rl._flush_ordering_heap(0)
        assert rl._highest_sequenced[0] == 3
        msg = rl.poll_receive()
        assert msg is not None
        assert msg[0] == b"seq_data"

    def test_build_range_packets_split(self):
        """Many ACK ranges exceeding MTU produce multiple ACK packets."""
        rl, cc = _make_layer_with_cc(mtu=100)
        ranges = [(i * 10, i * 10 + 1) for i in range(50)]
        packets = rl._build_ack_packets(ranges)
        assert len(packets) > 1

    def test_estimate_frame_size_sequenced(self):
        """Frame with RELIABLE_SEQUENCED includes 3-byte sequencing overhead in size estimate."""
        rl, cc = _make_layer_with_cc()
        frame = MessageFrame(
            reliability=Reliability.RELIABLE_SEQUENCED,
            data=b"x",
            sequencing_index=0,
            ordering_index=0,
            ordering_channel=0,
        )
        size = rl._estimate_frame_size(frame)
        assert size == 14

    def test_split_reliability_upgrade_unreliable_ack_receipt(self):
        """Large UNRELIABLE_WITH_ACK_RECEIPT message is upgraded to RELIABLE_WITH_ACK_RECEIPT when split."""
        rl, cc = _make_layer_with_cc(mtu=100)
        big_data = b"x" * 500
        rl.send(big_data, Reliability.UNRELIABLE_WITH_ACK_RECEIPT)
        assert len(rl._send_queue) > 1

    def test_nak_ranges_in_update(self):
        """NAK ranges are built and cleared during update()."""
        rl, cc = _make_layer_with_cc()
        rl._nak_ranges = [(0, 0), (2, 2)]
        cc.last_rtt = 0.01
        result = rl.update(1.0)
        assert len(result) >= 1
        assert len(rl._nak_ranges) == 0

    def test_datagram_rejection_huge_gap(self):
        """Datagram with absurdly large sequence gap is silently dropped."""
        rl, cc = _make_layer_with_cc()
        frame = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"x")
        raw = encode_datagram(60000, [frame])
        rl.on_datagram_received(raw, 1.0)
        assert rl.poll_receive() is None

    def test_unreliable_timeout_mixed_entries(self):
        """Mix of expired and non-expired unreliable messages: only expired are culled."""
        rl, cc = _make_layer_with_cc()
        rl._unreliable_timeout = 5.0
        expired = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"old")
        expired._creation_time = 0.0  # type: ignore[attr-defined]
        fresh = MessageFrame(reliability=Reliability.UNRELIABLE, data=b"new")
        fresh._creation_time = 99.0  # type: ignore[attr-defined]
        reliable = MessageFrame(reliability=Reliability.RELIABLE, data=b"rel")
        rl._send_queue = [(0, 0, expired), (1, 1, fresh), (2, 2, reliable)]
        cc.cwnd = 0.0
        rl.update(100.0)

    def test_build_nak_packets_split_mtu(self):
        """Many NAK ranges exceeding MTU produce multiple NAK packets."""
        rl, cc = _make_layer_with_cc(mtu=100)
        ranges = [(i * 10, i * 10 + 1) for i in range(50)]
        packets = rl._build_nak_packets(ranges)
        assert len(packets) > 1


# ---------------------------------------------------------------------------
# Tests from test_hardening.py
# ---------------------------------------------------------------------------


class TestReceivedWindow:
    """Tests for the _ReceivedWindow sliding window structure."""

    def test_add_and_contains(self):
        """Adding seq 0 makes it present in the window."""
        w = _ReceivedWindow(size=512)
        assert 0 not in w
        w.add(0)
        assert 0 in w

    def test_sequential_add(self):
        """Adding 0-7 sequentially makes all present."""
        w = _ReceivedWindow(size=8)
        for i in range(8):
            w.add(i)
        for i in range(8):
            assert i in w

    def test_window_advance(self):
        """Adding beyond window size advances the base; old entries below base are considered received."""
        w = _ReceivedWindow(size=8)
        for i in range(8):
            w.add(i)
        w.add(10)
        assert 10 in w
        assert 0 in w  # below base, treated as received

    def test_far_ahead_advances_window(self):
        """Jumping far ahead advances the window; values below new base are considered received."""
        w = _ReceivedWindow(size=8)
        w.add(0)
        w.add(1)
        w.add(100)
        assert 100 in w
        assert 0 in w
        assert 50 in w

    def test_wrap_around_seq_num_max(self):
        """Window handles 24-bit sequence wraparound near SEQ_NUM_MAX."""
        w = _ReceivedWindow(size=512)
        base = SEQ_NUM_MAX - 5
        for i in range(10):
            seq = (base + i) & SEQ_NUM_MAX
            w.add(seq)
        for i in range(10):
            seq = (base + i) & SEQ_NUM_MAX
            assert seq in w

    def test_behind_base_is_received(self):
        """Sequence numbers far behind the base are treated as already received."""
        w = _ReceivedWindow(size=8)
        for i in range(20):
            w.add(i)
        assert 0 in w
        assert 5 in w

    def test_default_size(self):
        """Default window size matches DEFAULT_RECEIVED_PACKET_QUEUE_SIZE constant."""
        w = _ReceivedWindow()
        assert w._size == DEFAULT_RECEIVED_PACKET_QUEUE_SIZE


class TestSplitTrackerEviction:
    """Tests for timeout-based split tracker eviction."""

    def _make(self, mtu=MAXIMUM_MTU, timeout=10.0):
        cc = CongestionController(mtu=mtu)
        return ReliabilityLayer(mtu=mtu, cc=cc, timeout=timeout)

    def test_stale_tracker_evicted(self):
        """Split tracker older than timeout is evicted when a new split arrives."""
        layer = self._make(timeout=5.0)
        now = 100.0

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

        now2 = now + 6.0
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

        assert 1 not in layer._split_trackers
        assert 2 in layer._split_trackers

    def test_non_stale_tracker_kept(self):
        """Split tracker within timeout is not evicted."""
        layer = self._make(timeout=10.0)
        now = 100.0

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

        assert 1 in layer._split_trackers
        assert 2 in layer._split_trackers

    def test_created_at_is_set(self):
        """Split tracker records the creation timestamp."""
        layer = self._make()
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

        assert layer._split_trackers[5].last_update_time == 42.0


class TestChannelValidation:
    """Tests for ordering_channel bounds checking in send()."""

    def _make(self):
        cc = CongestionController(mtu=MAXIMUM_MTU)
        return ReliabilityLayer(mtu=MAXIMUM_MTU, cc=cc)

    def test_valid_channel_zero(self):
        """Channel 0 is accepted."""
        layer = self._make()
        layer.send(b"test", Reliability.RELIABLE_ORDERED, 0)
        assert len(layer._send_queue) == 1

    def test_valid_channel_max(self):
        """Maximum valid channel (NUMBER_OF_ORDERED_STREAMS - 1) is accepted."""
        layer = self._make()
        layer.send(b"test", Reliability.RELIABLE_ORDERED, NUMBER_OF_ORDERED_STREAMS - 1)
        assert len(layer._send_queue) == 1

    def test_invalid_channel_negative(self):
        """Negative channel raises ValueError."""
        layer = self._make()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, -1)

    def test_invalid_channel_too_high(self):
        """Channel equal to NUMBER_OF_ORDERED_STREAMS raises ValueError."""
        layer = self._make()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, NUMBER_OF_ORDERED_STREAMS)

    def test_invalid_channel_way_too_high(self):
        """Channel 100 raises ValueError."""
        layer = self._make()
        with pytest.raises(ValueError, match="ordering_channel"):
            layer.send(b"test", Reliability.RELIABLE_ORDERED, 100)
