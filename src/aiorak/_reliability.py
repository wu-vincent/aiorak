"""Core reliability layer: ACK/NAK, ordering, sequencing, split reassembly.

Faithful port of ReliabilityLayer.cpp with message-level resend tracking,
duplicate detection, 24-bit wraparound, and MTU-limited ACK/NAK packets.
"""

import heapq
import logging
from collections import deque
from dataclasses import dataclass, field

from ._congestion import CongestionController, seq_greater_than
from ._constants import (
    DATAGRAM_MESSAGE_ID_ARRAY_LENGTH,
    DEFAULT_RECEIVED_PACKET_QUEUE_SIZE,
    DEFAULT_TIMEOUT,
    MAX_DATAGRAM_HISTORY_SIZE,
    MAX_ORDERING_HEAP_SIZE,
    MAX_RECEIVE_QUEUE_SIZE,
    MAX_RESEND_BUFFER_SIZE,
    MAX_SPLIT_PACKET_COUNT,
    MAX_SPLIT_TRACKERS,
    NUMBER_OF_ORDERED_STREAMS,
    SEQ_NUM_MAX,
    UDP_HEADER_SIZE,
)
from ._types import Priority, Reliability
from ._wire import (
    DatagramHeader,
    MessageFrame,
    decode_datagram,
    encode_ack,
    encode_datagram,
    encode_nak,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HALF_SEQ = (SEQ_NUM_MAX + 1) // 2

# Sub-key used for RELIABLE_ORDERED entries in the ordering heap.
# Sequenced entries use their sequencing_index (always < 1048575), so ordered
# entries sort *after* all sequenced entries with the same ordering_index.
# This matches the C++ weight scheme (ReliabilityLayer.cpp:1436-1446).
_ORDERED_SUBKEY = 1048575

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _InFlightMessage:
    """Per-message resend tracking (replaces per-datagram tracking).

    Each reliable message gets its own entry so that resends are message-level
    rather than datagram-level, matching C++ ReliabilityLayer behaviour.
    """

    reliable_message_number: int
    frame: MessageFrame
    next_action_time: float
    retransmission_time: float
    times_sent: int = 1
    byte_size: int = 0


@dataclass
class _DatagramRecord:
    """Maps a sent datagram seq to the reliable messages it contained."""

    seq: int
    send_time: float
    reliable_message_numbers: list[int] = field(default_factory=list)
    is_continuous_send: bool = False
    byte_size: int = 0


class _ReliableMessageWindow:
    """Sliding window for reliable message number deduplication.

    Ports C++ ``hasReceivedPacketQueue`` (``Queue<bool>``) from
    ``ReliabilityLayer.cpp:941-994``.
    """

    __slots__ = ("_base", "_window")

    def __init__(self) -> None:
        self._base: int = 0
        self._window: deque[bool] = deque()

    def check_and_mark(self, reliable_num: int) -> bool:
        """Return True if *reliable_num* is new, False if duplicate."""
        hole_count = (reliable_num - self._base) & SEQ_NUM_MAX

        # Behind the base (wrapped around) - duplicate
        if hole_count >= HALF_SEQ:
            return False

        # Safety cap: reject absurdly large gaps (C++ ReliabilityLayer.cpp:971)
        if hole_count > 1000000:
            return False

        if hole_count == 0:
            # Got exactly what we expected
            if self._window:
                self._window.popleft()
            self._base = (self._base + 1) & SEQ_NUM_MAX
            # Pop consecutive already-received entries
            while self._window and self._window[0]:
                self._window.popleft()
                self._base = (self._base + 1) & SEQ_NUM_MAX
            return True

        if hole_count < len(self._window):
            if self._window[hole_count]:
                return False  # Duplicate
            self._window[hole_count] = True
            return True

        # Extend the window to accommodate this message
        while len(self._window) < hole_count:
            self._window.append(False)
        self._window.append(True)
        return True


class _ReceivedWindow:
    """Sliding window tracker for received datagram sequence numbers.

    Packets below ``_base`` are considered already received.  Matches the C++
    ``hasReceivedPacketQueue`` approach.
    """

    __slots__ = ("_size", "_base", "_bits")

    def __init__(self, size: int = DEFAULT_RECEIVED_PACKET_QUEUE_SIZE) -> None:
        self._size = size
        self._base: int = 0
        self._bits: set[int] = set()

    def __contains__(self, seq: int) -> bool:
        offset = (seq - self._base) & SEQ_NUM_MAX
        if offset >= HALF_SEQ:
            return True
        if offset >= self._size:
            return False
        return seq in self._bits

    def add(self, seq: int) -> None:
        offset = (seq - self._base) & SEQ_NUM_MAX
        if offset >= HALF_SEQ:
            return
        if offset >= self._size:
            advance = offset - self._size + 1
            new_base = (self._base + advance) & SEQ_NUM_MAX
            to_remove = [s for s in self._bits if ((s - new_base) & SEQ_NUM_MAX) >= HALF_SEQ]
            for s in to_remove:
                self._bits.discard(s)
            self._base = new_base
        self._bits.add(seq)


@dataclass
class _SplitTracker:
    """Tracks fragments of a single split message until reassembly."""

    total: int
    received: dict[int, bytes] = field(default_factory=dict)
    reliability: Reliability = Reliability.RELIABLE
    ordering_index: int = 0
    ordering_channel: int = 0
    last_update_time: float = 0.0


class ReliabilityLayer:
    """Per-connection reliability layer.

    Manages ACK/NAK generation, message ordering, split-packet reassembly,
    and the resend buffer for a single peer connection.
    """

    def __init__(self, mtu: int, cc: CongestionController, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._mtu = mtu
        self._cc = cc
        self._timeout = timeout

        # --- Send-side counters ---
        self._next_reliable_num: int = 0
        self._next_ordering_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS
        self._next_sequencing_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS
        self._next_split_id: int = 0

        # --- Send queue (priority heap) ---
        # Each entry is (weight, counter, frame) for priority-weighted scheduling
        # matching C++ ReliabilityLayer.cpp:3925-3945.
        self._send_queue: list[tuple[int, int, MessageFrame]] = []
        self._send_queue_counter: int = 0
        self._priority_next_weights: list[int] = [0] * 4
        self._init_priority_weights()

        # --- Message-level resend buffer (keyed by reliable_message_number) ---
        self._resend_buffer: dict[int, _InFlightMessage] = {}

        # --- Datagram history (datagram seq -> record) ---
        self._datagram_history: dict[int, _DatagramRecord] = {}
        self._unacked_bytes: int = 0

        # --- Receive-side state ---
        self._ack_ranges: list[tuple[int, int]] = []
        self._nak_ranges: list[tuple[int, int]] = []
        self._received_datagrams: _ReceivedWindow = _ReceivedWindow()

        # Reliable message deduplication
        self._reliable_message_window: _ReliableMessageWindow = _ReliableMessageWindow()

        # Ordering heaps per channel.
        # Each entry is (ordering_index, sub_key, counter, is_ordered, data).
        #   sub_key = sequencing_index for sequenced, _ORDERED_SUBKEY for ordered.
        # This weighted scheme matches C++ ReliabilityLayer.cpp:1436-1446.
        self._ordering_heaps: list[list[tuple[int, int, int, bool, bytes]]] = [
            [] for _ in range(NUMBER_OF_ORDERED_STREAMS)
        ]
        self._heap_counter: int = 0
        self._expected_ordering_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS

        # Sequencing: highest seen index per channel (-1 = never seen)
        self._highest_sequenced: list[int] = [-1] * NUMBER_OF_ORDERED_STREAMS

        # Split reassembly
        self._split_trackers: dict[int, _SplitTracker] = {}

        # Completed messages ready for the application
        self._receive_queue: list[tuple[bytes, int]] = []

        # Track last reliable send time for keepalive (C++ lastReliableSend)
        self._last_reliable_send: float = 0.0

    def _init_priority_weights(self) -> None:
        """Initialize priority heap weights matching C++ InitHeapWeights.

        The C++ scheme uses ``(1<<p)*p+p`` as the base step for each priority
        level, ensuring that higher-priority messages (lower ``p``) get smaller
        weights and thus sort first in the min-heap.  Over many messages this
        gives a fair 2:1 ratio between adjacent levels.
        """
        for p in range(4):
            self._priority_next_weights[p] = (1 << p) * p + p

    def _get_next_weight(self, priority: int) -> int:
        """Return the next heap weight for *priority*, advancing the counter.

        Matches C++ ``ReliabilityLayer::GetNextWeight``.
        """
        w = self._priority_next_weights[priority]
        step = (1 << priority) * (priority + 1) + priority
        self._priority_next_weights[priority] = w + step
        return w

    @property
    def has_pending_data(self) -> bool:
        """True if there are frames queued, in-flight, or awaiting resend."""
        return bool(self._send_queue or self._resend_buffer or self._datagram_history)

    # ------------------------------------------------------------------
    # Public send API
    # ------------------------------------------------------------------

    def send(
        self,
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        ordering_channel: int = 0,
        priority: Priority = Priority.MEDIUM,
    ) -> None:
        """Enqueue a message for transmission.

        Large messages are automatically split into fragments that fit within
        the negotiated MTU.  Split packets are upgraded to reliable delivery
        matching C++ ``ReliabilityLayer.cpp:1608-1621``.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            ordering_channel: Ordering channel (0–31).
            priority: Send priority (IMMEDIATE/HIGH/MEDIUM/LOW).
        """
        if not (0 <= ordering_channel < NUMBER_OF_ORDERED_STREAMS):
            raise ValueError(f"ordering_channel must be in [0, {NUMBER_OF_ORDERED_STREAMS}), got {ordering_channel}")
        max_payload = self._max_payload_per_frame(reliability)

        if len(data) <= max_payload:
            frame = self._build_frame(data, reliability, ordering_channel)
            self._enqueue_frame(frame, priority)
        else:
            # Split reliability upgrade (C++ ReliabilityLayer.cpp:1608-1621)
            if reliability == Reliability.UNRELIABLE:
                reliability = Reliability.RELIABLE
            elif reliability == Reliability.UNRELIABLE_WITH_ACK_RECEIPT:
                reliability = Reliability.RELIABLE_WITH_ACK_RECEIPT
            elif reliability == Reliability.UNRELIABLE_SEQUENCED:
                reliability = Reliability.RELIABLE_SEQUENCED
            self._split_and_queue(data, reliability, ordering_channel, max_payload, priority)

    def _enqueue_frame(self, frame: MessageFrame, priority: Priority = Priority.MEDIUM) -> None:
        """Push a frame onto the priority send queue."""
        weight = self._get_next_weight(priority.value)
        self._send_queue_counter += 1
        heapq.heappush(self._send_queue, (weight, self._send_queue_counter, frame))

    # ------------------------------------------------------------------
    # Incoming datagram processing
    # ------------------------------------------------------------------

    def on_datagram_received(self, raw: bytes, now: float) -> None:
        """Process a raw UDP datagram from the remote peer."""
        try:
            header, body = decode_datagram(raw)
        except ValueError:
            logger.debug("Dropping malformed datagram (%d bytes)", len(raw))
            return

        if header.is_ack:
            self._handle_ack(body, now)  # type: ignore[arg-type]
        elif header.is_nak:
            self._handle_nak(body)  # type: ignore[arg-type]
        elif header.is_data:
            self._handle_data(header, body, now)  # type: ignore[arg-type]

    def poll_receive(self) -> tuple[bytes, int] | None:
        """Pop the next completed message from the receive queue."""
        if self._receive_queue:
            return self._receive_queue.pop(0)
        return None

    def _deliver(self, data: bytes, channel: int) -> None:
        """Append a completed message to the receive queue if not full."""
        if len(self._receive_queue) < MAX_RECEIVE_QUEUE_SIZE:
            self._receive_queue.append((data, channel))

    # ------------------------------------------------------------------
    # Periodic update - build & send datagrams
    # ------------------------------------------------------------------

    def update(self, now: float) -> list[bytes]:
        """Run a periodic update tick and return datagrams to send."""
        outgoing: list[bytes] = []

        # 1. ACKs (MTU-limited)
        if self._ack_ranges and self._cc.should_send_acks(now):
            outgoing.extend(self._build_ack_packets(self._ack_ranges))
            self._ack_ranges.clear()
            self._cc.on_send_ack()

        # 2. NAKs (MTU-limited)
        if self._nak_ranges:
            outgoing.extend(self._build_nak_packets(self._nak_ranges))
            self._nak_ranges.clear()

        # 3. Message-level RTO resends
        rto = self._cc.get_rto()
        resend_frames: list[MessageFrame] = []
        for msg in self._resend_buffer.values():
            if now >= msg.next_action_time:
                resend_frames.append(msg.frame)

        if resend_frames:
            self._cc.on_resend()

        # 4. Evict stale split trackers
        stale = [k for k, t in self._split_trackers.items() if now - t.last_update_time > self._timeout]
        for k in stale:
            del self._split_trackers[k]

        # 5. Evict very old datagram history entries
        stale_dg = [seq for seq, rec in self._datagram_history.items() if now - rec.send_time > self._timeout]
        for seq in stale_dg:
            rec = self._datagram_history.pop(seq)
            self._unacked_bytes -= rec.byte_size

        # 6. Build datagrams
        # C++ DatagramHeaderFormat::GetDataHeaderByteLength() = 2 + 3 + sizeof(float) = 9
        datagram_overhead = 9
        max_datagram_payload = self._mtu - UDP_HEADER_SIZE - datagram_overhead

        # 6a. Pack resend frames (bypass cwnd - separate retransmission bandwidth)
        if resend_frames:
            outgoing.extend(self._pack_datagrams(resend_frames, max_datagram_payload, now, rto, is_resend=True))

        # 6b. Pack new send frames (cwnd-limited), extracted from priority heap
        if self._send_queue:
            new_frames: list[MessageFrame] = []
            while self._send_queue:
                _w, _c, frame = heapq.heappop(self._send_queue)
                new_frames.append(frame)
            outgoing.extend(self._pack_datagrams(new_frames, max_datagram_payload, now, rto, is_resend=False))

        return outgoing

    def _pack_datagrams(
        self,
        frames: list[MessageFrame],
        max_payload: int,
        now: float,
        rto: float,
        *,
        is_resend: bool,
    ) -> list[bytes]:
        """Pack frames into datagrams with late reliable number assignment."""
        outgoing: list[bytes] = []
        idx = 0
        sent_at_least_one = False

        while idx < len(frames):
            if not is_resend:
                budget = self._cc.transmission_bandwidth(self._unacked_bytes, len(frames) - idx > 1)
                if budget <= 0 and sent_at_least_one:
                    # Re-enqueue remaining frames onto the priority heap
                    for f in frames[idx:]:
                        self._send_queue_counter += 1
                        heapq.heappush(self._send_queue, (0, self._send_queue_counter, f))
                    break

            frames_for_dg: list[MessageFrame] = []
            dg_size = 0

            while idx < len(frames) and dg_size < max_payload:
                frame = frames[idx]
                frame_size = self._estimate_frame_size(frame)
                if dg_size + frame_size > max_payload and frames_for_dg:
                    break
                frames_for_dg.append(frame)
                dg_size += frame_size
                idx += 1

            if not frames_for_dg:
                break

            # Late reliable message number assignment
            reliable_nums: list[int] = []
            for frame in frames_for_dg:
                if frame.reliability.is_reliable and frame.reliable_message_number == -1:
                    frame.reliable_message_number = self._next_reliable_num
                    self._next_reliable_num = (self._next_reliable_num + 1) & SEQ_NUM_MAX
                if frame.reliability.is_reliable:
                    reliable_nums.append(frame.reliable_message_number)

            is_continuous = (len(frames) - idx > 0) or bool(self._send_queue)
            seq = self._cc.get_and_increment_seq()
            raw = encode_datagram(seq, frames_for_dg, is_continuous_send=is_continuous)

            # Evict oldest datagram history entry if at capacity
            if len(self._datagram_history) >= MAX_DATAGRAM_HISTORY_SIZE:
                oldest_seq = min(self._datagram_history, key=lambda s: self._datagram_history[s].send_time)
                rec = self._datagram_history.pop(oldest_seq)
                self._unacked_bytes -= rec.byte_size

            # Record datagram history
            self._datagram_history[seq] = _DatagramRecord(
                seq=seq,
                send_time=now,
                reliable_message_numbers=reliable_nums,
                is_continuous_send=is_continuous,
                byte_size=len(raw),
            )
            self._unacked_bytes += len(raw)

            # Update resend buffer for reliable messages and track last reliable send
            for frame in frames_for_dg:
                if frame.reliability.is_reliable:
                    self._last_reliable_send = now
                    rmn = frame.reliable_message_number
                    existing = self._resend_buffer.get(rmn)
                    if existing is not None:
                        # Resend - update tracking with exponential backoff
                        existing.next_action_time = now + rto * (2 ** min(existing.times_sent, 5))
                        existing.retransmission_time = rto
                        existing.times_sent += 1
                    elif len(self._resend_buffer) < MAX_RESEND_BUFFER_SIZE:
                        # New message
                        self._resend_buffer[rmn] = _InFlightMessage(
                            reliable_message_number=rmn,
                            frame=frame,
                            next_action_time=now + rto,
                            retransmission_time=rto,
                            byte_size=self._estimate_frame_size(frame),
                        )

            outgoing.append(raw)
            sent_at_least_one = True

        return outgoing

    # ------------------------------------------------------------------
    # ACK / NAK handling
    # ------------------------------------------------------------------

    def _handle_ack(self, ranges: list[tuple[int, int]], now: float) -> None:
        """Process acknowledged datagram sequence numbers.

        Uses ``_datagram_history`` to find which reliable messages were in each
        datagram, and passes the stored ``is_continuous_send`` to the congestion
        controller instead of reading stale state.
        """
        total_processed = 0
        for lo, hi in ranges:
            count = ((hi - lo) & SEQ_NUM_MAX) + 1
            count = min(count, DATAGRAM_MESSAGE_ID_ARRAY_LENGTH)
            seq = lo
            for _ in range(count):
                if total_processed >= DATAGRAM_MESSAGE_ID_ARRAY_LENGTH:
                    return
                total_processed += 1
                rec = self._datagram_history.pop(seq, None)
                if rec is not None:
                    rtt = now - rec.send_time
                    self._unacked_bytes -= rec.byte_size
                    self._cc.on_ack(rtt, seq, rec.is_continuous_send)
                    # Remove acknowledged messages from resend buffer
                    for rmn in rec.reliable_message_numbers:
                        self._resend_buffer.pop(rmn, None)
                seq = (seq + 1) & SEQ_NUM_MAX

    def _handle_nak(self, ranges: list[tuple[int, int]]) -> None:
        """Process NAKs - mark affected messages for immediate resend.

        Unlike ACK handling, NAK does NOT remove the datagram history entry.
        The datagram may still arrive later and be ACK'd, so the record must
        remain for proper congestion controller bookkeeping (C++
        ReliabilityLayer.cpp:831-856).
        """
        total_processed = 0
        for lo, hi in ranges:
            count = ((hi - lo) & SEQ_NUM_MAX) + 1
            count = min(count, DATAGRAM_MESSAGE_ID_ARRAY_LENGTH)
            seq = lo
            for _ in range(count):
                if total_processed >= DATAGRAM_MESSAGE_ID_ARRAY_LENGTH:
                    return
                total_processed += 1
                rec = self._datagram_history.get(seq)
                if rec is not None:
                    self._cc.on_nak()
                    # Mark messages for immediate resend
                    for rmn in rec.reliable_message_numbers:
                        msg = self._resend_buffer.get(rmn)
                        if msg is not None:
                            msg.next_action_time = 0.0
                seq = (seq + 1) & SEQ_NUM_MAX

    # ------------------------------------------------------------------
    # Data datagram handling
    # ------------------------------------------------------------------

    def _handle_data(
        self,
        header: DatagramHeader,
        frames: list[MessageFrame],
        now: float,
    ) -> None:
        """Process an incoming data datagram.

        Performs datagram-level deduplication before processing frames.
        """
        seq = header.datagram_number
        skipped = self._cc.on_got_packet(seq, now)

        # Reject packet if gap is absurdly large (C++ returns false)
        if skipped is None:
            return

        # Record for ACK
        self._add_to_ranges(self._ack_ranges, seq)

        # Generate NAKs for skipped datagrams
        if skipped > 0:
            expected = (seq - skipped) & SEQ_NUM_MAX
            for i in range(skipped):
                missed = (expected + i) & SEQ_NUM_MAX
                if missed not in self._received_datagrams:
                    self._add_to_ranges(self._nak_ranges, missed)

        # Datagram-level dedup: ACK it but skip frame processing
        if seq in self._received_datagrams:
            return

        self._received_datagrams.add(seq)

        # Process each message frame
        for frame in frames:
            self._handle_frame(frame, now)

    def _handle_frame(self, frame: MessageFrame, now: float) -> None:
        """Process a single decoded message frame.

        Performs reliable message deduplication, split reassembly, then routes
        through ordering/sequencing logic.
        """
        # Reliable message number deduplication (C++ hasReceivedPacketQueue)
        if frame.reliability.is_reliable:
            if not self._reliable_message_window.check_and_mark(frame.reliable_message_number):
                return  # Duplicate

        # Split reassembly
        if frame.split_packet_count > 0:
            data = self._reassemble_split(frame, now)
            if data is None:
                return
            frame = MessageFrame(
                reliability=frame.reliability,
                data=data,
                data_bit_length=len(data) * 8,
                reliable_message_number=frame.reliable_message_number,
                sequencing_index=frame.sequencing_index,
                ordering_index=frame.ordering_index,
                ordering_channel=frame.ordering_channel,
            )

        rel = frame.reliability
        ch = frame.ordering_channel

        if rel.is_sequenced:
            # C++ checks orderingIndex FIRST for sequenced packets
            # (ReliabilityLayer.cpp:1283-1341).
            if frame.ordering_index == self._expected_ordering_index[ch]:
                # Current ordering slot - check sequencing index
                if self._highest_sequenced[ch] == -1 or seq_greater_than(
                    frame.sequencing_index & SEQ_NUM_MAX,
                    self._highest_sequenced[ch] & SEQ_NUM_MAX,
                ):
                    self._highest_sequenced[ch] = frame.sequencing_index
                    self._deliver(frame.data, ch)
                # else: stale sequencing index, drop
            elif seq_greater_than(
                frame.ordering_index & SEQ_NUM_MAX,
                self._expected_ordering_index[ch] & SEQ_NUM_MAX,
            ):
                # Future ordering slot - buffer in ordering heap (bounded)
                if len(self._ordering_heaps[ch]) < MAX_ORDERING_HEAP_SIZE:
                    self._heap_counter += 1
                    heapq.heappush(
                        self._ordering_heaps[ch],
                        (frame.ordering_index, frame.sequencing_index, self._heap_counter, False, frame.data),
                    )
                else:
                    logger.warning("Ordering heap channel %d full, dropping frame", ch)
            # else: stale ordering index, drop

        elif rel.is_ordered:
            # RELIABLE_ORDERED (and RELIABLE_ORDERED_WITH_ACK_RECEIPT on wire)
            if frame.ordering_index == self._expected_ordering_index[ch]:
                self._deliver(frame.data, ch)
                self._expected_ordering_index[ch] = (self._expected_ordering_index[ch] + 1) & SEQ_NUM_MAX
                self._highest_sequenced[ch] = -1
                self._flush_ordering_heap(ch)
            elif seq_greater_than(
                frame.ordering_index & SEQ_NUM_MAX,
                self._expected_ordering_index[ch] & SEQ_NUM_MAX,
            ):
                if len(self._ordering_heaps[ch]) < MAX_ORDERING_HEAP_SIZE:
                    self._heap_counter += 1
                    heapq.heappush(
                        self._ordering_heaps[ch],
                        (frame.ordering_index, _ORDERED_SUBKEY, self._heap_counter, True, frame.data),
                    )
                else:
                    logger.warning("Ordering heap channel %d full, dropping frame", ch)
            # else: stale ordering index, silently drop

        else:
            # UNRELIABLE, RELIABLE, *_WITH_ACK_RECEIPT - deliver directly
            self._deliver(frame.data, ch)

    def _flush_ordering_heap(self, channel: int) -> None:
        """Deliver buffered ordered/sequenced messages that are now sequential.

        Matches C++ ReliabilityLayer.cpp:1372-1423: when popping from the
        ordering heap, ordered items advance ``orderedReadIndex`` and reset
        the sequencing counter, while sequenced items only update the
        highest-seen sequencing index.
        """
        heap = self._ordering_heaps[channel]
        while heap and heap[0][0] == self._expected_ordering_index[channel]:
            _oi, _sub, _cnt, is_ordered, data = heapq.heappop(heap)
            self._deliver(data, channel)
            if is_ordered:
                self._expected_ordering_index[channel] = (self._expected_ordering_index[channel] + 1) & SEQ_NUM_MAX
                self._highest_sequenced[channel] = -1
            else:
                # Sequenced: update highest seen but do NOT advance ordering index
                self._highest_sequenced[channel] = _sub  # _sub is sequencing_index

    # ------------------------------------------------------------------
    # Split packet reassembly
    # ------------------------------------------------------------------

    def _reassemble_split(self, frame: MessageFrame, now: float) -> bytes | None:
        """Accumulate a split fragment and return the reassembled payload when complete."""
        sid = frame.split_packet_id
        if frame.split_packet_count > MAX_SPLIT_PACKET_COUNT:
            return None
        tracker = self._split_trackers.get(sid)
        if tracker is None:
            stale = [k for k, t in self._split_trackers.items() if now - t.last_update_time > self._timeout]
            for k in stale:
                logger.warning("Evicting stale split tracker id=%d", k)
                del self._split_trackers[k]

            # Enforce hard limit on concurrent split reassembly sessions
            if len(self._split_trackers) >= MAX_SPLIT_TRACKERS:
                oldest_key = min(self._split_trackers, key=lambda k: self._split_trackers[k].last_update_time)
                logger.warning("Split tracker limit reached, evicting id=%d", oldest_key)
                del self._split_trackers[oldest_key]

            tracker = _SplitTracker(
                total=frame.split_packet_count,
                reliability=frame.reliability,
                ordering_index=frame.ordering_index,
                ordering_channel=frame.ordering_channel,
                last_update_time=now,
            )
            self._split_trackers[sid] = tracker

        # Defense-in-depth: reject out-of-range indices even though
        # _wire.py already validates split_index < split_count on decode.
        if frame.split_packet_index >= tracker.total:
            return None

        tracker.received[frame.split_packet_index] = frame.data
        tracker.last_update_time = now

        if len(tracker.received) < tracker.total:
            return None

        # Verify all indices 0..N-1 are present before reassembly
        for i in range(tracker.total):
            if i not in tracker.received:
                return None

        logger.debug("Split reassembly complete: id=%d, %d fragments", sid, tracker.total)
        parts = [tracker.received[i] for i in range(tracker.total)]
        del self._split_trackers[sid]
        return b"".join(parts)

    # ------------------------------------------------------------------
    # Frame building helpers
    # ------------------------------------------------------------------

    def _advance_ordering_counters(
        self,
        reliability: Reliability,
        channel: int,
    ) -> tuple[int, int]:
        """Consume and return ordering/sequencing counters with 24-bit wraparound."""
        ordering_index = 0
        sequencing_index = 0

        if reliability.is_sequenced:
            sequencing_index = self._next_sequencing_index[channel]
            self._next_sequencing_index[channel] = (self._next_sequencing_index[channel] + 1) & SEQ_NUM_MAX

        if reliability.is_ordered:
            ordering_index = self._next_ordering_index[channel]
            if not reliability.is_sequenced:
                self._next_ordering_index[channel] = (self._next_ordering_index[channel] + 1) & SEQ_NUM_MAX
                self._next_sequencing_index[channel] = 0

        return ordering_index, sequencing_index

    def _build_frame(
        self,
        data: bytes,
        reliability: Reliability,
        channel: int,
    ) -> MessageFrame:
        """Build a single MessageFrame with late reliable number assignment."""
        frame = MessageFrame(
            reliability=reliability,
            data=data,
            data_bit_length=len(data) * 8,
            ordering_channel=channel,
        )

        # Late assignment: set to -1, assigned when packing into datagram
        if reliability.is_reliable:
            frame.reliable_message_number = -1

        ordering_index, sequencing_index = self._advance_ordering_counters(reliability, channel)
        frame.ordering_index = ordering_index
        frame.sequencing_index = sequencing_index

        return frame

    def _split_and_queue(
        self,
        data: bytes,
        reliability: Reliability,
        channel: int,
        max_payload: int,
        priority: Priority = Priority.MEDIUM,
    ) -> None:
        """Split a large message into MTU-sized fragments and enqueue them.

        Reliability has already been upgraded by the caller if needed.
        """
        split_id = self._next_split_id
        self._next_split_id = (self._next_split_id + 1) & 0xFFFF

        fragments: list[bytes] = []
        offset = 0
        while offset < len(data):
            fragments.append(data[offset : offset + max_payload])
            offset += max_payload

        total = len(fragments)
        ordering_index, sequencing_index = self._advance_ordering_counters(reliability, channel)

        for i, chunk in enumerate(fragments):
            frame = MessageFrame(
                reliability=reliability,
                data=chunk,
                data_bit_length=len(chunk) * 8,
                reliable_message_number=-1 if reliability.is_reliable else 0,
                sequencing_index=sequencing_index,
                ordering_index=ordering_index,
                ordering_channel=channel,
                split_packet_count=total,
                split_packet_id=split_id,
                split_packet_index=i,
            )
            self._enqueue_frame(frame, priority)

    # ------------------------------------------------------------------
    # MTU-limited ACK/NAK packets
    # ------------------------------------------------------------------

    def _build_ack_packets(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Build ACK packets, splitting across multiple if exceeding MTU."""
        return self._build_range_packets(ranges, is_ack=True)

    def _build_nak_packets(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Build NAK packets, splitting across multiple if exceeding MTU."""
        return self._build_range_packets(ranges, is_ack=False)

    def _build_range_packets(self, ranges: list[tuple[int, int]], *, is_ack: bool) -> list[bytes]:
        """Split a range list into MTU-sized ACK or NAK packets."""
        max_size = self._mtu - UDP_HEADER_SIZE
        # Header overhead: 1 byte (flags) + 2 bytes (range count) = 3 bytes
        header_overhead = 3

        packets: list[bytes] = []
        current_ranges: list[tuple[int, int]] = []
        current_size = header_overhead

        for lo, hi in ranges:
            range_size = 4 if lo == hi else 7
            if current_size + range_size > max_size and current_ranges:
                if is_ack:
                    packets.append(encode_ack(current_ranges))
                else:
                    packets.append(encode_nak(current_ranges))
                current_ranges = []
                current_size = header_overhead

            current_ranges.append((lo, hi))
            current_size += range_size

        if current_ranges:
            if is_ack:
                packets.append(encode_ack(current_ranges))
            else:
                packets.append(encode_nak(current_ranges))

        return packets

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _max_payload_per_frame(self, reliability: Reliability) -> int:
        """Calculate the maximum user-data bytes per message frame.

        Overhead breakdown: UDP header + datagram header (9 bytes, C++
        DatagramHeaderFormat) + max message frame header (13 bytes) + max
        split header (10 bytes).
        """
        overhead = UDP_HEADER_SIZE + 9 + 13 + 10
        return max(self._mtu - overhead, 64)

    @staticmethod
    def _add_to_ranges(ranges: list[tuple[int, int]], value: int) -> None:
        """Insert *value* into a sorted range list, merging adjacent entries."""
        ranges.append((value, value))
        ranges.sort()
        merged: list[tuple[int, int]] = []
        for lo, hi in ranges:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        ranges.clear()
        ranges.extend(merged)

    def _estimate_frame_size(self, frame: MessageFrame) -> int:
        """Estimate the wire size of a message frame in bytes."""
        size = 3 + len(frame.data)
        if frame.reliability.is_reliable:
            size += 3
        if frame.reliability.is_sequenced:
            size += 3
        if frame.reliability.is_ordered:
            size += 4
        if frame.split_packet_count > 0:
            size += 10
        return size
