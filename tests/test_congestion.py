"""Unit tests for CongestionController and sequence number wrapping."""


import pytest

from aiorak._congestion import CongestionController, seq_greater_than, seq_less_than
from aiorak._constants import SEQ_NUM_MAX


class TestSequenceComparison:
    def test_seq_greater_than_basic(self):
        assert seq_greater_than(5, 3) is True
        assert seq_greater_than(3, 5) is False
        assert seq_greater_than(5, 5) is False

    def test_seq_greater_than_wrap(self):
        # 0 wraps past 0xFFFFFF, so 0 > 0xFFFFFF in circular space
        assert seq_greater_than(0, SEQ_NUM_MAX) is True
        assert seq_greater_than(1, SEQ_NUM_MAX) is True
        assert seq_greater_than(SEQ_NUM_MAX, 0) is False

    def test_seq_less_than(self):
        assert seq_less_than(3, 5) is True
        assert seq_less_than(5, 3) is False
        assert seq_less_than(5, 5) is False

    def test_seq_less_than_wrap(self):
        assert seq_less_than(SEQ_NUM_MAX, 0) is True
        assert seq_less_than(0, SEQ_NUM_MAX) is False

    def test_seq_half_space_boundary(self):
        half = 0x800000
        # At exactly half distance, neither is greater
        assert seq_greater_than(half, 0) is False
        assert seq_greater_than(0, half) is False


class TestCongestionControllerInit:
    def test_initial_cwnd_equals_mtu(self):
        cc = CongestionController(mtu=1200)
        assert cc.cwnd == 1200.0
        assert cc.mtu == 1200

    def test_initial_state(self):
        cc = CongestionController(mtu=1492)
        assert cc.ss_thresh == 0.0
        assert cc.next_datagram_seq == 0
        assert cc.expected_next_seq == 0


class TestSlowStart:
    def test_slow_start_growth(self):
        cc = CongestionController(mtu=1000)
        initial_cwnd = cc.cwnd
        # Simulate an ACK in slow start (ss_thresh == 0 means always slow start)
        seq = cc.get_and_increment_seq()
        cc.on_ack(rtt=0.05, seq=seq, is_continuous=True)
        assert cc.cwnd == initial_cwnd + cc.mtu


class TestCongestionAvoidance:
    def test_congestion_avoidance_growth(self):
        cc = CongestionController(mtu=1000)
        # Force out of slow start by setting ss_thresh
        cc.ss_thresh = 1000.0
        cc.cwnd = 5000.0
        old_cwnd = cc.cwnd
        seq = cc.get_and_increment_seq()
        cc.on_ack(rtt=0.05, seq=seq, is_continuous=True)
        # In congestion avoidance: cwnd += mtu*mtu/cwnd
        expected = old_cwnd + (cc.mtu * cc.mtu / old_cwnd)
        assert abs(cc.cwnd - expected) < 1.0


class TestOnResend:
    def test_on_resend_halves_window(self):
        cc = CongestionController(mtu=500)
        cc.cwnd = 5000.0
        cc._is_continuous_send = True
        old_cwnd = cc.cwnd
        cc.on_resend()
        assert cc.ss_thresh == old_cwnd / 2
        assert cc.cwnd == float(cc.mtu)

    def test_on_resend_only_once_per_block(self):
        cc = CongestionController(mtu=500)
        cc.cwnd = 5000.0
        cc._is_continuous_send = True
        cc.on_resend()
        cwnd_after_first = cc.cwnd
        # Second resend in same block should be no-op
        cc.on_resend()
        assert cc.cwnd == cwnd_after_first


class TestRTO:
    def test_rto_formula(self):
        cc = CongestionController(mtu=1000)
        cc.estimated_rtt = 0.050
        cc.deviation_rtt = 0.010
        expected_rto = 2.0 * 0.050 + 4.0 * 0.010 + 0.03
        assert abs(cc.get_rto() - expected_rto) < 1e-9

    def test_rto_unset_returns_max(self):
        cc = CongestionController(mtu=1000)
        assert cc.get_rto() == 2.0

    def test_rto_capped_at_2(self):
        cc = CongestionController(mtu=1000)
        cc.estimated_rtt = 1.0
        cc.deviation_rtt = 1.0
        # 2*1 + 4*1 + 0.03 = 6.03, should be capped at 2.0
        assert cc.get_rto() == 2.0


class TestOnGotPacket:
    def test_on_got_packet_in_order(self):
        cc = CongestionController(mtu=1000)
        gap = cc.on_got_packet(0, now=1.0)
        assert gap == 0
        assert cc.expected_next_seq == 1

    def test_on_got_packet_gap_detection(self):
        cc = CongestionController(mtu=1000)
        cc.on_got_packet(0, now=1.0)
        # Skip seq 1 and 2
        gap = cc.on_got_packet(3, now=1.1)
        assert gap == 2
        assert cc.expected_next_seq == 4

    def test_on_got_packet_duplicate(self):
        cc = CongestionController(mtu=1000)
        cc.on_got_packet(0, now=1.0)
        cc.on_got_packet(1, now=1.0)
        # Receiving 0 again (duplicate/old)
        gap = cc.on_got_packet(0, now=1.1)
        assert gap == 0


class TestGetAndIncrementSeq:
    def test_sequential(self):
        cc = CongestionController(mtu=1000)
        assert cc.get_and_increment_seq() == 0
        assert cc.get_and_increment_seq() == 1
        assert cc.get_and_increment_seq() == 2

    def test_wraps_at_max(self):
        cc = CongestionController(mtu=1000)
        cc.next_datagram_seq = SEQ_NUM_MAX
        assert cc.get_and_increment_seq() == SEQ_NUM_MAX
        assert cc.get_and_increment_seq() == 0
