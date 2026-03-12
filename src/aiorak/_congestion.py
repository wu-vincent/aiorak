"""TCP-like sliding window congestion control.

This is a Python re-implementation of the algorithm found in
``CCRakNetSlidingWindow.cpp``.  It tracks round-trip time (RTT) with an
exponentially weighted moving average, maintains a congestion window
(``cwnd``), and implements slow-start / congestion-avoidance transitions.

The controller is **not** thread-safe; it is designed to be driven by a
single asyncio task per connection.
"""

from ._constants import SEQ_NUM_MAX, SYN_INTERVAL

_UNSET: float = -1.0


def seq_greater_than(a: int, b: int) -> bool:
    """Return ``True`` if sequence number *a* is logically greater than *b*.

    Uses 24-bit wrapping arithmetic so that the comparison is correct even
    when the sequence space wraps around ``0xFFFFFF → 0``.

    Args:
        a: First 24-bit sequence number.
        b: Second 24-bit sequence number.

    Returns:
        ``True`` if *a* > *b* in the circular sequence space.
    """
    half = 0x800000  # (SEQ_NUM_MAX + 1) // 2
    return a != b and ((a - b) & SEQ_NUM_MAX) < half


def seq_less_than(a: int, b: int) -> bool:
    """Return ``True`` if sequence number *a* is logically less than *b*.

    Complement of :func:`seq_greater_than`.
    """
    return seq_greater_than(b, a)


class CongestionController:
    """Sliding-window congestion controller for a single connection.

    The algorithm mirrors ``CCRakNetSlidingWindow`` from the C++ codebase:

    * **Slow start**: ``cwnd`` increases by ``mtu`` per ACK until
      ``cwnd > ss_thresh``.
    * **Congestion avoidance**: ``cwnd`` increases by
      ``mtu * mtu / cwnd`` per congestion-control period.
    * **On loss/resend**: ``ss_thresh = cwnd / 2``, ``cwnd = mtu``.
    * **RTO**: ``2 * estimated_rtt + 4 * deviation_rtt + 0.03``,
      capped at 2.0 s.

    Args:
        mtu: Maximum transmission unit (bytes) for this connection.
    """

    __slots__ = (
        "mtu",
        "cwnd",
        "ss_thresh",
        "estimated_rtt",
        "deviation_rtt",
        "last_rtt",
        "next_datagram_seq",
        "expected_next_seq",
        "oldest_unsent_ack",
        "_next_cc_block",
        "_backed_off_this_block",
        "_is_continuous_send",
    )

    def __init__(self, mtu: int) -> None:
        self.mtu: int = mtu
        self.cwnd: float = float(mtu)
        self.ss_thresh: float = 0.0
        self.estimated_rtt: float = _UNSET
        self.deviation_rtt: float = _UNSET
        self.last_rtt: float = _UNSET
        self.next_datagram_seq: int = 0
        self.expected_next_seq: int = 0
        self.oldest_unsent_ack: float = 0.0
        self._next_cc_block: int = 0
        self._backed_off_this_block: bool = False
        self._is_continuous_send: bool = False

    # ------------------------------------------------------------------
    # Sequence numbers
    # ------------------------------------------------------------------

    def get_and_increment_seq(self) -> int:
        """Return the next datagram sequence number and increment the counter.

        Returns:
            The 24-bit datagram sequence number to use for the next outgoing
            datagram.
        """
        seq = self.next_datagram_seq
        self.next_datagram_seq = (self.next_datagram_seq + 1) & SEQ_NUM_MAX
        return seq

    # ------------------------------------------------------------------
    # Bandwidth queries
    # ------------------------------------------------------------------

    def transmission_bandwidth(self, unacked_bytes: int, is_continuous: bool) -> int:
        """Return the number of bytes that can be sent right now.

        Args:
            unacked_bytes: Total bytes currently in-flight (unacknowledged).
            is_continuous: Whether the application has a steady stream of data.

        Returns:
            Allowed send budget in bytes (may be 0 if the window is full).
        """
        self._is_continuous_send = is_continuous
        if unacked_bytes <= self.cwnd:
            return int(self.cwnd - unacked_bytes)
        return 0

    def retransmission_bandwidth(self, unacked_bytes: int) -> int:
        """Return the number of bytes available for retransmission.

        C++ ``CCRakNetSlidingWindow::GetRetransmissionBandwidth`` returns
        ``unacknowledgedBytes`` - effectively unlimited retransmission.

        Args:
            unacked_bytes: Total bytes currently in-flight (unacknowledged).

        Returns:
            Allowed retransmission budget in bytes.
        """
        return unacked_bytes

    # ------------------------------------------------------------------
    # ACK handling
    # ------------------------------------------------------------------

    def should_send_acks(self, now: float) -> bool:
        """Return ``True`` if accumulated ACKs should be flushed.

        ACKs are delayed by one *SYN* interval (10 ms) unless the RTT is
        unknown, in which case they are sent immediately.

        Args:
            now: Current monotonic time in seconds.
        """
        if self.last_rtt == _UNSET:
            return True
        return self.oldest_unsent_ack > 0.0 and now >= self.oldest_unsent_ack + SYN_INTERVAL

    def on_got_packet(self, seq: int, now: float) -> int | None:
        """Record reception of a data datagram and return the gap count.

        Args:
            seq: Datagram sequence number received.
            now: Current monotonic time in seconds.

        Returns:
            Number of datagrams skipped (for NAK generation), 0 if this is
            the expected next sequence number, or ``None`` if the packet
            should be rejected entirely (gap > 50 000, matching C++).
        """
        if self.oldest_unsent_ack == 0.0:
            self.oldest_unsent_ack = now

        if seq == self.expected_next_seq:
            self.expected_next_seq = (seq + 1) & SEQ_NUM_MAX
            return 0

        if seq_greater_than(seq, self.expected_next_seq):
            skipped = (seq - self.expected_next_seq) & SEQ_NUM_MAX
            if skipped > 50000:
                return None  # reject packet (C++ CCRakNetSlidingWindow.cpp:147)
            skipped = min(skipped, 1000)
            self.expected_next_seq = (seq + 1) & SEQ_NUM_MAX
            return skipped

        return 0  # duplicate / old packet

    def on_ack(self, rtt: float, seq: int, is_continuous: bool) -> None:
        """Process an acknowledged datagram.

        Updates RTT estimates and adjusts the congestion window.

        Args:
            rtt: Measured round-trip time in seconds for this datagram.
            seq: Datagram sequence number that was acknowledged.
            is_continuous: Whether the sender is in continuous-send mode.
        """
        self.last_rtt = rtt

        if self.estimated_rtt == _UNSET:
            self.estimated_rtt = rtt
            self.deviation_rtt = rtt
        else:
            d = 0.05
            diff = rtt - self.estimated_rtt
            self.estimated_rtt += d * diff
            self.deviation_rtt += d * (abs(diff) - self.deviation_rtt)

        self._is_continuous_send = is_continuous
        if not is_continuous:
            return

        is_new_period = seq_greater_than(seq, self._next_cc_block)
        if is_new_period:
            self._backed_off_this_block = False
            self._next_cc_block = self.next_datagram_seq

        if self._is_in_slow_start():
            self.cwnd += self.mtu
            if self.ss_thresh > 0 and self.cwnd > self.ss_thresh:
                self.cwnd = self.ss_thresh + self.mtu * self.mtu / self.cwnd
        elif is_new_period:
            self.cwnd += self.mtu * self.mtu / self.cwnd

    def on_resend(self) -> None:
        """Respond to a packet being retransmitted (RTO or NAK-triggered).

        Cuts the congestion window to one MTU and enters slow start, but
        only once per congestion-control block to avoid over-reacting.
        """
        if self._is_continuous_send and not self._backed_off_this_block and self.cwnd > self.mtu * 2:
            self.ss_thresh = max(self.cwnd / 2, float(self.mtu))
            self.cwnd = float(self.mtu)
            self._next_cc_block = self.next_datagram_seq
            self._backed_off_this_block = True

    def on_nak(self) -> None:
        """Respond to a NAK for a datagram.

        Sets the slow-start threshold to half the current window without
        immediately shrinking ``cwnd`` (the resend path handles that).
        """
        if self._is_continuous_send and not self._backed_off_this_block:
            self.ss_thresh = self.cwnd / 2

    def on_send_ack(self) -> None:
        """Reset the ACK delay timer after flushing ACKs."""
        self.oldest_unsent_ack = 0.0

    # ------------------------------------------------------------------
    # RTO
    # ------------------------------------------------------------------

    def get_rto(self) -> float:
        """Return the retransmission timeout in seconds.

        Formula: ``2 * estimated_rtt + 4 * deviation_rtt + 0.03``, capped
        at 2.0 s.  Returns 2.0 s if no RTT sample has been collected yet.

        Returns:
            Retransmission timeout in seconds.
        """
        if self.estimated_rtt == _UNSET:
            return 2.0
        rto = 2.0 * self.estimated_rtt + 4.0 * self.deviation_rtt + 0.03
        return min(rto, 2.0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_in_slow_start(self) -> bool:
        """Return ``True`` if the controller is in the slow-start phase."""
        return self.cwnd <= self.ss_thresh or self.ss_thresh == 0.0
