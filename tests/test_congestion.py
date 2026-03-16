"""Unit tests for the CongestionController sliding window."""

from aiorak._congestion import CongestionController, seq_less_than


class TestSeqLessThan:
    def test_less(self):
        """seq_less_than(5, 10) returns True for normal ordering."""
        assert seq_less_than(5, 10) is True

    def test_greater(self):
        """seq_less_than(10, 5) returns False when first is greater."""
        assert seq_less_than(10, 5) is False

    def test_equal(self):
        """seq_less_than(5, 5) returns False for equal values."""
        assert seq_less_than(5, 5) is False


class TestCongestionController:
    def test_retransmission_bandwidth(self):
        """retransmission_bandwidth returns min of unacked_bytes and cwnd."""
        cc = CongestionController(1000)
        assert cc.retransmission_bandwidth(1000) == 1000
        assert cc.retransmission_bandwidth(0) == 0

    def test_on_got_packet_huge_gap(self):
        """Receiving a packet with a huge sequence gap returns None (rejected)."""
        cc = CongestionController(1000)
        cc.expected_next_seq = 0
        result = cc.on_got_packet(60000, 1.0)
        assert result is None

    def test_on_nak_continuous_send(self):
        """NAK during continuous send halves ss_thresh."""
        cc = CongestionController(1000)
        cc._is_continuous_send = True
        cc._backed_off_this_block = False
        cc.cwnd = 10000.0
        cc.on_nak()
        assert cc.ss_thresh == 5000.0
