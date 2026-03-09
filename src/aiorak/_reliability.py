"""Core reliability layer: ACK/NAK, ordering, sequencing, split reassembly.

This module implements the heart of the RakNet reliable-UDP protocol.  The
:class:`ReliabilityLayer` manages:

* **Send side** — queueing outgoing messages, auto-incrementing reliable /
  ordering / sequencing counters, splitting messages that exceed the MTU,
  and building datagrams that respect the congestion window.
* **Receive side** — detecting gaps for NAK generation, building ACK range
  lists, reassembling split packets, enforcing ordering per channel with a
  heap, and discarding stale sequenced packets.
* **Resend buffer** — tracking in-flight datagrams so they can be
  retransmitted on NAK or RTO expiry.

The layer is driven by a periodic ``update()`` call (typically every 10 ms)
and by ``on_datagram_received()`` for inbound traffic.
"""

import heapq
import time as _time
from dataclasses import dataclass, field

from ._bitstream import BitStream
from ._congestion import CongestionController, seq_greater_than
from ._constants import (
    MAXIMUM_MTU,
    NUMBER_OF_ORDERED_STREAMS,
    SEQ_NUM_MAX,
    UDP_HEADER_SIZE,
)
from ._types import Reliability
from ._wire import (
    DatagramHeader,
    MessageFrame,
    decode_datagram,
    encode_ack,
    encode_datagram,
    encode_message_frame,
    encode_nak,
)


# ---------------------------------------------------------------------------
# Internal packet tracking
# ---------------------------------------------------------------------------

@dataclass
class _InFlightDatagram:
    """Metadata for a datagram that has been sent but not yet ACKed.

    Attributes:
        seq: Datagram sequence number.
        send_time: Monotonic timestamp (seconds) when the datagram was sent.
        frames: Message frames contained in this datagram.
        byte_size: Total encoded byte size of the datagram.
        times_sent: How many times this datagram has been (re)transmitted.
    """

    seq: int
    send_time: float
    frames: list[MessageFrame]
    byte_size: int
    times_sent: int = 1


@dataclass
class _SplitTracker:
    """Tracks fragments of a single split message until reassembly.

    Attributes:
        total: Expected total number of fragments.
        received: Mapping of fragment index → payload bytes.
        reliability: Reliability mode of the original message.
        ordering_index: Ordering index (if ordered).
        ordering_channel: Ordering channel (if ordered).
    """

    total: int
    received: dict[int, bytes] = field(default_factory=dict)
    reliability: Reliability = Reliability.RELIABLE
    ordering_index: int = 0
    ordering_channel: int = 0


class ReliabilityLayer:
    """Per-connection reliability layer.

    Manages ACK/NAK generation, message ordering, split-packet reassembly,
    and the resend buffer for a single peer connection.

    Args:
        mtu: Negotiated maximum transmission unit for this connection.
        cc: The :class:`~aiorak._congestion.CongestionController` instance
            shared with this connection.
    """

    def __init__(self, mtu: int, cc: CongestionController) -> None:
        self._mtu = mtu
        self._cc = cc

        # --- Send-side counters ---
        self._next_reliable_num: int = 0
        self._next_ordering_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS
        self._next_sequencing_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS
        self._next_split_id: int = 0

        # --- Send queue & resend buffer ---
        self._send_queue: list[MessageFrame] = []
        self._resend_queue: list[MessageFrame] = []
        self._in_flight: dict[int, _InFlightDatagram] = {}
        self._unacked_bytes: int = 0

        # --- Receive-side state ---
        self._ack_ranges: list[tuple[int, int]] = []
        self._nak_ranges: list[tuple[int, int]] = []
        self._received_datagrams: set[int] = set()

        # Ordering heaps per channel: list of (ordering_index, data_bytes)
        self._ordering_heaps: list[list[tuple[int, bytes]]] = [
            [] for _ in range(NUMBER_OF_ORDERED_STREAMS)
        ]
        self._expected_ordering_index: list[int] = [0] * NUMBER_OF_ORDERED_STREAMS

        # Sequencing: highest seen index per channel
        self._highest_sequenced: list[int] = [-1] * NUMBER_OF_ORDERED_STREAMS

        # Split reassembly
        self._split_trackers: dict[int, _SplitTracker] = {}

        # Completed messages ready for the application
        self._receive_queue: list[tuple[bytes, int]] = []

    @property
    def has_pending_data(self) -> bool:
        """True if there are frames queued, in-flight, or awaiting resend."""
        return bool(self._send_queue or self._resend_queue or self._in_flight)

    # ------------------------------------------------------------------
    # Public send API
    # ------------------------------------------------------------------

    def send(
        self,
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
        ordering_channel: int = 0,
    ) -> None:
        """Enqueue a message for transmission.

        Large messages are automatically split into fragments that fit within
        the negotiated MTU.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee for this message.
            ordering_channel: Ordering channel (0–31) for ordered/sequenced modes.
        """
        max_payload = self._max_payload_per_frame(reliability)

        if len(data) <= max_payload:
            frame = self._build_frame(data, reliability, ordering_channel)
            self._send_queue.append(frame)
        else:
            # Split into fragments
            self._split_and_queue(data, reliability, ordering_channel, max_payload)

    # ------------------------------------------------------------------
    # Incoming datagram processing
    # ------------------------------------------------------------------

    def on_datagram_received(self, raw: bytes, now: float) -> None:
        """Process a raw UDP datagram from the remote peer.

        Handles ACK, NAK, and data datagrams.  Reassembled / ordered
        messages are placed on the internal receive queue.

        Args:
            raw: Raw datagram bytes (the full UDP payload).
            now: Current monotonic time in seconds.
        """
        header, body = decode_datagram(raw)

        if header.is_ack:
            self._handle_ack(body, now)  # type: ignore[arg-type]
        elif header.is_nak:
            self._handle_nak(body)  # type: ignore[arg-type]
        elif header.is_data:
            self._handle_data(header, body, now)  # type: ignore[arg-type]

    def poll_receive(self) -> tuple[bytes, int] | None:
        """Pop the next completed message from the receive queue.

        Returns:
            ``(data, channel)`` tuple, or ``None`` if the queue is empty.
        """
        if self._receive_queue:
            return self._receive_queue.pop(0)
        return None

    # ------------------------------------------------------------------
    # Periodic update — build & send datagrams
    # ------------------------------------------------------------------

    def update(self, now: float) -> list[bytes]:
        """Run a periodic update tick and return datagrams to send.

        This should be called every ~10 ms.  It:

        1. Flushes pending ACKs/NAKs.
        2. Checks for RTO-expired in-flight datagrams and re-queues them.
        3. Builds new data datagrams from the send queue, respecting the
           congestion window.

        Args:
            now: Current monotonic time in seconds.

        Returns:
            A list of raw datagram bytes to transmit over UDP.
        """
        outgoing: list[bytes] = []

        # 1. ACKs
        if self._ack_ranges and self._cc.should_send_acks(now):
            outgoing.append(encode_ack(self._ack_ranges))
            self._ack_ranges.clear()
            self._cc.on_send_ack()

        # 2. NAKs
        if self._nak_ranges:
            outgoing.append(encode_nak(self._nak_ranges))
            self._nak_ranges.clear()

        # 3. RTO resends
        rto = self._cc.get_rto()
        expired_seqs: list[int] = []
        for seq, inflight in self._in_flight.items():
            if now - inflight.send_time >= rto:
                expired_seqs.append(seq)

        for seq in expired_seqs:
            inflight = self._in_flight.pop(seq)
            self._unacked_bytes -= inflight.byte_size
            self._cc.on_resend()
            # Re-queue frames for retransmission
            self._resend_queue.extend(inflight.frames)

        # 4. Build data datagrams from resend + send queues
        combined = self._resend_queue + self._send_queue
        self._resend_queue.clear()
        self._send_queue.clear()

        # Datagram overhead: 1 byte header bits + 3 bytes seq num = ~4 bytes
        datagram_overhead = 4
        max_datagram_payload = self._mtu - UDP_HEADER_SIZE - datagram_overhead

        idx = 0
        sent_at_least_one = False
        while idx < len(combined):
            budget = self._cc.transmission_bandwidth(self._unacked_bytes, len(combined) > 1)
            if budget <= 0 and sent_at_least_one:
                # Re-queue remaining frames
                self._send_queue = combined[idx:] + self._send_queue
                break

            frames_for_dg: list[MessageFrame] = []
            dg_size = 0

            while idx < len(combined) and dg_size < max_datagram_payload:
                frame = combined[idx]
                # Estimate frame wire size
                frame_size = self._estimate_frame_size(frame)
                if dg_size + frame_size > max_datagram_payload and frames_for_dg:
                    break
                frames_for_dg.append(frame)
                dg_size += frame_size
                idx += 1

            if not frames_for_dg:
                break

            seq = self._cc.get_and_increment_seq()
            raw = encode_datagram(
                seq,
                frames_for_dg,
                is_continuous_send=len(combined) - idx > 0,
            )
            self._in_flight[seq] = _InFlightDatagram(
                seq=seq,
                send_time=now,
                frames=frames_for_dg,
                byte_size=len(raw),
            )
            self._unacked_bytes += len(raw)
            outgoing.append(raw)
            sent_at_least_one = True

        return outgoing

    # ------------------------------------------------------------------
    # ACK / NAK handling
    # ------------------------------------------------------------------

    def _handle_ack(self, ranges: list[tuple[int, int]], now: float) -> None:
        """Process acknowledged datagram sequence numbers.

        Args:
            ranges: List of ``(min, max)`` acknowledged sequence number ranges.
            now: Current monotonic time.
        """
        for lo, hi in ranges:
            seq = lo
            while seq <= hi:
                inflight = self._in_flight.pop(seq, None)
                if inflight is not None:
                    rtt = now - inflight.send_time
                    self._unacked_bytes -= inflight.byte_size
                    self._cc.on_ack(rtt, seq, self._cc._is_continuous_send)
                seq = (seq + 1) & SEQ_NUM_MAX

    def _handle_nak(self, ranges: list[tuple[int, int]]) -> None:
        """Process negative acknowledgements — requeue affected datagrams.

        Args:
            ranges: List of ``(min, max)`` missing sequence number ranges.
        """
        for lo, hi in ranges:
            seq = lo
            while seq <= hi:
                inflight = self._in_flight.pop(seq, None)
                if inflight is not None:
                    self._unacked_bytes -= inflight.byte_size
                    self._cc.on_nak()
                    self._cc.on_resend()
                    self._resend_queue.extend(inflight.frames)
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

        Generates ACK entries and NAK ranges for gaps, then delivers each
        contained message frame.

        Args:
            header: Parsed datagram header.
            frames: List of decoded message frames.
            now: Current monotonic time.
        """
        seq = header.datagram_number
        skipped = self._cc.on_got_packet(seq, now)

        # Record for ACK
        self._add_to_ranges(self._ack_ranges, seq)

        # Generate NAKs for skipped datagrams
        if skipped > 0:
            expected = (seq - skipped) & SEQ_NUM_MAX
            for i in range(skipped):
                missed = (expected + i) & SEQ_NUM_MAX
                if missed not in self._received_datagrams:
                    self._add_to_ranges(self._nak_ranges, missed)

        self._received_datagrams.add(seq)

        # Process each message frame
        for frame in frames:
            self._handle_frame(frame)

    def _handle_frame(self, frame: MessageFrame) -> None:
        """Process a single decoded message frame.

        Handles split reassembly, then routes complete messages through
        ordering/sequencing logic.

        Args:
            frame: The decoded message frame.
        """
        # Split reassembly
        if frame.split_packet_count > 0:
            data = self._reassemble_split(frame)
            if data is None:
                return  # Not all fragments received yet
            # Use the first fragment's metadata for the reassembled message
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

        if not rel.is_ordered:
            self._receive_queue.append((frame.data, 0))

        elif rel.is_sequenced:
            ch = frame.ordering_channel
            if frame.sequencing_index > self._highest_sequenced[ch]:
                self._highest_sequenced[ch] = frame.sequencing_index
                self._receive_queue.append((frame.data, ch))
            # else: stale packet, drop silently

        else:  # ordered (non-sequenced)
            ch = frame.ordering_channel
            if frame.ordering_index == self._expected_ordering_index[ch]:
                self._receive_queue.append((frame.data, ch))
                self._expected_ordering_index[ch] += 1
                self._highest_sequenced[ch] = -1  # Reset per C++ behavior
                # Flush any buffered messages that are now in order
                self._flush_ordering_heap(ch)
            else:
                heapq.heappush(
                    self._ordering_heaps[ch],
                    (frame.ordering_index, frame.data),
                )

    def _flush_ordering_heap(self, channel: int) -> None:
        """Deliver buffered ordered messages that are now sequential.

        Args:
            channel: The ordering channel to flush.
        """
        heap = self._ordering_heaps[channel]
        while heap and heap[0][0] == self._expected_ordering_index[channel]:
            _, data = heapq.heappop(heap)
            self._receive_queue.append((data, channel))
            self._expected_ordering_index[channel] += 1
            self._highest_sequenced[channel] = -1  # Reset per C++ behavior

    # ------------------------------------------------------------------
    # Split packet reassembly
    # ------------------------------------------------------------------

    def _reassemble_split(self, frame: MessageFrame) -> bytes | None:
        """Accumulate a split fragment and return the reassembled payload when complete.

        Args:
            frame: A message frame with ``split_packet_count > 0``.

        Returns:
            The full reassembled payload bytes, or ``None`` if not all
            fragments have arrived yet.
        """
        sid = frame.split_packet_id
        tracker = self._split_trackers.get(sid)
        if tracker is None:
            tracker = _SplitTracker(
                total=frame.split_packet_count,
                reliability=frame.reliability,
                ordering_index=frame.ordering_index,
                ordering_channel=frame.ordering_channel,
            )
            self._split_trackers[sid] = tracker

        tracker.received[frame.split_packet_index] = frame.data

        if len(tracker.received) < tracker.total:
            return None

        # All fragments received — reassemble in order
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
        """Consume and return ordering/sequencing counters for a message.

        This is the single source of truth for counter advancement, called
        by both :meth:`_build_frame` (single messages) and
        :meth:`_split_and_queue` (split fragments that share one set of
        counters).

        Args:
            reliability: Delivery guarantee.
            channel: Ordering channel (0–31).

        Returns:
            ``(ordering_index, sequencing_index)`` to stamp on the frame(s).
        """
        ordering_index = 0
        sequencing_index = 0

        if reliability.is_sequenced:
            sequencing_index = self._next_sequencing_index[channel]
            self._next_sequencing_index[channel] += 1

        if reliability.is_ordered:
            ordering_index = self._next_ordering_index[channel]
            if not reliability.is_sequenced:
                # Ordered (non-sequenced) modes consume the ordering index
                # and reset the sequencing counter per C++ behavior.
                self._next_ordering_index[channel] += 1
                self._next_sequencing_index[channel] = 0

        return ordering_index, sequencing_index

    def _build_frame(
        self,
        data: bytes,
        reliability: Reliability,
        channel: int,
    ) -> MessageFrame:
        """Build a single :class:`MessageFrame` with appropriate counters.

        Args:
            data: Raw payload bytes.
            reliability: Delivery guarantee.
            channel: Ordering channel (0–31).

        Returns:
            A fully populated :class:`MessageFrame`.
        """
        frame = MessageFrame(
            reliability=reliability,
            data=data,
            data_bit_length=len(data) * 8,
            ordering_channel=channel,
        )

        if reliability.is_reliable:
            frame.reliable_message_number = self._next_reliable_num
            self._next_reliable_num += 1

        ordering_index, sequencing_index = self._advance_ordering_counters(
            reliability, channel,
        )
        frame.ordering_index = ordering_index
        frame.sequencing_index = sequencing_index

        return frame

    def _split_and_queue(
        self,
        data: bytes,
        reliability: Reliability,
        channel: int,
        max_payload: int,
    ) -> None:
        """Split a large message into MTU-sized fragments and enqueue them.

        Args:
            data: Full payload bytes.
            reliability: Delivery guarantee for all fragments.
            channel: Ordering channel.
            max_payload: Maximum payload bytes per fragment.
        """
        split_id = self._next_split_id & 0xFFFF
        self._next_split_id += 1

        fragments: list[bytes] = []
        offset = 0
        while offset < len(data):
            fragments.append(data[offset: offset + max_payload])
            offset += max_payload

        total = len(fragments)
        ordering_index, sequencing_index = self._advance_ordering_counters(
            reliability, channel,
        )

        for i, chunk in enumerate(fragments):
            frame = MessageFrame(
                reliability=reliability,
                data=chunk,
                data_bit_length=len(chunk) * 8,
                reliable_message_number=self._next_reliable_num if reliability.is_reliable else 0,
                sequencing_index=sequencing_index,
                ordering_index=ordering_index,
                ordering_channel=channel,
                split_packet_count=total,
                split_packet_id=split_id,
                split_packet_index=i,
            )
            if reliability.is_reliable:
                self._next_reliable_num += 1
            self._send_queue.append(frame)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _max_payload_per_frame(self, reliability: Reliability) -> int:
        """Calculate the maximum user-data bytes per message frame.

        Subtracts the datagram header, frame header, and split-packet
        overhead from the MTU.

        Args:
            reliability: Delivery mode (affects header size).

        Returns:
            Maximum payload bytes that fit in a single frame.
        """
        # Datagram overhead: ~4 bytes (header bits + seq number)
        # Frame header: 1 byte (rel+split) + 2 (data bit len) + 3 (reliable num)
        #   + 3 (seq index) + 3 (order index) + 1 (channel) = ~13 bytes worst case
        # Split fields: 4 + 2 + 4 = 10 bytes
        overhead = UDP_HEADER_SIZE + 4 + 13 + 10
        return max(self._mtu - overhead, 64)

    @staticmethod
    def _add_to_ranges(ranges: list[tuple[int, int]], value: int) -> None:
        """Insert *value* into a sorted range list, merging adjacent entries.

        This maintains the compact representation used for ACK/NAK encoding.

        Args:
            ranges: The range list to modify in-place.
            value: The sequence number to insert.
        """
        # Simple approach: append and sort/merge
        # For a production system a more efficient structure would be used
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
        """Estimate the wire size of a message frame in bytes.

        Args:
            frame: The frame to estimate.

        Returns:
            Approximate encoded byte count.
        """
        # 1 byte header (reliability + split bit + alignment)
        # 2 bytes data bit length
        size = 3 + len(frame.data)
        if frame.reliability.is_reliable:
            size += 3  # reliable message number
        if frame.reliability.is_sequenced:
            size += 3  # sequencing index
        if frame.reliability.is_ordered:
            size += 4  # ordering index + channel
        if frame.split_packet_count > 0:
            size += 10  # split fields
        return size
