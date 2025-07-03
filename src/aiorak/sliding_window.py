def is_later(a: int, b: int) -> bool:
    """Return True if a is after b in 24-bit sequence space."""
    return a != b and ((a - b) & 0xFFFFFF) < 0x7FFFFF


class SlidingWindow:
    """
    Sliding-window congestion-control for reliable UDP, time units in seconds.

    Implements slow-start and congestion-avoidance cwnd adjustments
    using RTT measurements and loss signals.

    Parameters:
        max_mtu (int): Maximum payload size per datagram (bytes).

    Attributes:
        max_mtu (int): Configured MTU (bytes).
        _last_rtt (float | None): Last RTT measurement (s).
        _estimated_rtt (float | None): Smoothed RTT estimate (s).
        _deviation_rtt (float | None): RTT variance estimate (s).
        _cwnd (float): Congestion window size (bytes).
        _ss_thresh (float): Slow-start threshold (bytes).
        _next_datagram_id (int): Next datagram id to assign.
        _expected_next_id (int): Next expected incoming.
        _continuous_send (bool): Continuous send mode flag.
    """

    DATAGRAM_ID_MASK = 0xFFFFFF

    def __init__(self, max_mtu: int):
        self.max_mtu: int = max_mtu
        self._last_rtt: float | None = None
        self._estimated_rtt: float | None = None
        self._deviation_rtt: float | None = None
        self._cwnd: float = float(max_mtu)
        self._ss_thresh: float = 0.0
        self._next_datagram_id: int = 0
        self._next_cc_block: int = 0
        self._backoff_this_block: bool = False
        self._speedup_this_block: bool = False
        self._expected_next_id: int = 0
        self._continuous_send: bool = False

    def get_transmission_bandwidth(self, unacked_bytes: int, is_continuous: bool) -> int:
        """Bytes allowed to send under current cwnd."""
        self._continuous_send = is_continuous
        return max(int(self._cwnd - unacked_bytes), 0)

    def next_datagram_id(self) -> int:
        """Get and advance the datagram id."""
        seq = self._next_datagram_id
        self._next_datagram_id = (seq + 1) & self.DATAGRAM_ID_MASK
        return seq

    def on_got_packet(self, datagram_id: int) -> int:
        """
        Process an incoming packet, schedule ACK, report skips.

        Returns:
            skipped_count (int)
        """
        datagram_id &= self.DATAGRAM_ID_MASK
        if datagram_id == self._expected_next_id:
            skipped = 0
        elif is_later(datagram_id, self._expected_next_id):
            skipped = (datagram_id - expected) & self.DATAGRAM_ID_MASK
            assert skipped <= 1000, "Too many skipped packets"
        else:
            # Duplicate or out-of-order; already received
            skipped = 0

        self._expected_next_id = (datagram_id + 1) & self.DATAGRAM_ID_MASK
        return skipped

    def on_resend(self) -> None:
        """Handle retransmission timeout: back off cwnd."""
        if self._continuous_send and not self._backoff_this_block and self._cwnd > self.max_mtu * 2:
            self._ss_thresh = max(self._cwnd / 2, self.max_mtu)
            self._cwnd = float(self.max_mtu)
            self._next_cc_block = self._next_datagram_id
            self._backoff_this_block = True

    def on_nak(self) -> None:
        """Handle NAK: enter congestion avoidance."""
        if self._continuous_send and not self._backoff_this_block:
            self._ss_thresh = self._cwnd / 2

    def on_ack(self, rtt: float, is_continuous: bool, datagram_id: int) -> None:
        """Update RTT estimates and adjust cwnd on ACK."""
        self._last_rtt = rtt
        if self._estimated_rtt is None:
            self._estimated_rtt = self._deviation_rtt = rtt
        else:
            d = 0.05
            diff = rtt - self._estimated_rtt
            self._estimated_rtt += d * diff
            self._deviation_rtt += d * (abs(diff) - self._deviation_rtt)

        self._continuous_send = is_continuous
        if not is_continuous:
            return

        new_block = is_later(datagram_id, self._next_cc_block)
        if new_block:
            self._backoff_this_block = False
            self._speedup_this_block = False
            self._next_cc_block = self._next_datagram_id

        if self.is_in_slow_start:
            self._cwnd += self.max_mtu
            if self._ss_thresh and self._cwnd > self._ss_thresh:
                self._cwnd = self._ss_thresh + (self.max_mtu ** 2) / self._cwnd
        elif new_block:
            self._cwnd += (self.max_mtu ** 2) / self._cwnd

    def get_rto_for_retransmission(self) -> float:
        """Calculate retransmission timeout in seconds."""
        max_thr = 2.0
        add_var = 0.03
        if self._estimated_rtt is None:
            return max_thr
        u, q = 2.0, 4.0
        return min(u * self._estimated_rtt + q * self._deviation_rtt + add_var, max_thr)

    def get_rtt(self) -> float:
        """Return last RTT measurement in seconds."""
        return self._last_rtt or 0.0

    @property
    def is_in_slow_start(self) -> bool:
        return self._cwnd <= self._ss_thresh or self._ss_thresh == 0
