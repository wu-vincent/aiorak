"""Unit tests for the ReliabilityLayer: send/receive, split/reassembly, ordering."""

import time

from aiorak._congestion import CongestionController
from aiorak._constants import MAXIMUM_MTU
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

        # The datagram should be in-flight
        assert len(layer._in_flight) == 1

        # Feed an ACK back for datagram 0
        ack = encode_ack([(0, 0)])
        layer.on_datagram_received(ack, now + 0.01)

        # In-flight should be cleared
        assert len(layer._in_flight) == 0

    def test_nak_triggers_resend(self):
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)

        # Feed a NAK for datagram 0
        nak = encode_nak([(0, 0)])
        layer.on_datagram_received(nak, now + 0.01)

        # The frame should be re-queued for resend
        assert len(layer._resend_queue) >= 1

    def test_rto_expiry_triggers_resend(self):
        layer = _make_layer()
        layer.send(b"\x86data")
        now = time.monotonic()
        layer.update(now)

        assert len(layer._in_flight) == 1

        # Advance time past RTO (default is 2.0s when no RTT sample)
        future = now + 3.0
        datagrams = layer.update(future)
        # The expired frame should have been re-sent
        # Either it's in the resend queue or already produced in datagrams
        assert len(datagrams) >= 1 or len(layer._resend_queue) >= 1
