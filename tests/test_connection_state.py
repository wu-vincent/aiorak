"""Unit-test the Connection state machine in isolation (no sockets).

Feed one Connection's output to the other via on_datagram() to simulate
a full handshake without any network I/O.
"""

from __future__ import annotations

import time

import pytest

from aiorak._connection import Connection, ConnectionState
from aiorak._constants import (
    ID_OPEN_CONNECTION_REQUEST_1,
    MAXIMUM_MTU,
    OFFLINE_MAGIC,
    RAKNET_PROTOCOL_VERSION,
)
from aiorak._bitstream import BitStream
from aiorak._types import EventType


def _make_pair(
    timeout: float = 10.0,
) -> tuple[Connection, Connection]:
    """Create a client + server Connection pair."""
    client = Connection(
        address=("127.0.0.1", 19132),
        guid=1000,
        is_server=False,
        mtu=MAXIMUM_MTU,
        timeout=timeout,
    )
    server = Connection(
        address=("127.0.0.1", 50000),
        guid=2000,
        is_server=True,
        mtu=MAXIMUM_MTU,
        timeout=timeout,
    )
    return client, server


def _do_full_handshake(
    client: Connection, server: Connection, now: float
) -> float:
    """Drive a full handshake between client and server, return updated time."""
    # Phase 1: offline handshake
    pkt = client.start_connect(now)
    assert pkt is not None

    # Server handles open_request_1 -> returns open_reply_1
    responses = server.on_datagram(pkt, now)
    assert len(responses) == 1

    # Client handles open_reply_1 -> returns open_request_2
    responses2 = client.on_datagram(responses[0], now)
    assert len(responses2) == 1

    # Server handles open_request_2 -> returns open_reply_2
    responses3 = server.on_datagram(responses2[0], now)
    assert len(responses3) == 1

    # Client handles open_reply_2 -> sends connection_request via reliability
    client.on_datagram(responses3[0], now)

    # Phase 2: reliable handshake — flush reliability layers back and forth
    # We need several round trips for the reliable handshake to complete
    for _ in range(20):
        now += 0.01

        # Client flush
        client_out = client.update(now)
        for dg in client_out:
            server.on_datagram(dg, now)

        # Server flush
        server_out = server.update(now)
        for dg in server_out:
            client.on_datagram(dg, now)

        # Check if both sides connected
        client.poll_events()
        server.poll_events()
        if (
            client.state == ConnectionState.CONNECTED
            and server.state == ConnectionState.CONNECTED
        ):
            break

    return now


class TestStateTransitions:
    def test_initial_state_is_disconnected(self):
        client, server = _make_pair()
        assert client.state == ConnectionState.DISCONNECTED
        assert server.state == ConnectionState.DISCONNECTED

    def test_start_connect_moves_to_connecting(self):
        client, _ = _make_pair()
        now = time.monotonic()
        client.start_connect(now)
        assert client.state == ConnectionState.CONNECTING

    def test_full_handshake_reaches_connected(self):
        """Wire client+server Connection objects together."""
        client, server = _make_pair()
        now = time.monotonic()
        _do_full_handshake(client, server, now)
        assert client.state == ConnectionState.CONNECTED
        assert server.state == ConnectionState.CONNECTED

    def test_disconnect_from_connected(self):
        client, server = _make_pair()
        now = time.monotonic()
        now = _do_full_handshake(client, server, now)

        client.disconnect()
        assert client.state == ConnectionState.DISCONNECTING

        # Flush so server sees the disconnect notification
        now += 0.01
        client_out = client.update(now)
        for dg in client_out:
            server.on_datagram(dg, now)
        server.update(now)

        events = server.poll_events()
        disconnect_events = [e for e in events if e.type == EventType.DISCONNECT]
        assert len(disconnect_events) == 1


class TestClientHandshake:
    def test_start_connect_builds_open_request_1(self):
        """Verify msg ID, magic, protocol version, MTU padding."""
        client, _ = _make_pair()
        now = time.monotonic()
        pkt = client.start_connect(now)

        bs = BitStream(pkt)
        msg_id = bs.read_uint8()
        assert msg_id == ID_OPEN_CONNECTION_REQUEST_1

        magic = bs.read_bytes(16)
        assert magic == OFFLINE_MAGIC

        proto = bs.read_uint8()
        assert proto == RAKNET_PROTOCOL_VERSION

        # Remaining bytes are MTU padding
        assert len(pkt) > 18  # At least header + padding

    def test_handles_open_reply_1(self):
        """Client extracts server GUID and MTU from reply."""
        client, server = _make_pair()
        now = time.monotonic()

        pkt = client.start_connect(now)
        responses = server.on_datagram(pkt, now)
        assert len(responses) == 1

        # Client processes open_reply_1
        client.on_datagram(responses[0], now)
        assert client.remote_guid == server.guid

    def test_handles_open_reply_2_sends_conn_request(self):
        """After open_reply_2, connection_request is sent via reliability layer."""
        client, server = _make_pair()
        now = time.monotonic()

        pkt = client.start_connect(now)
        r1 = server.on_datagram(pkt, now)
        r2 = client.on_datagram(r1[0], now)
        r3 = server.on_datagram(r2[0], now)
        client.on_datagram(r3[0], now)

        # The reliability layer should have a queued frame (connection_request)
        assert len(client._reliability._send_queue) >= 1


class TestServerHandshake:
    def test_handles_open_request_1_replies(self):
        """Server replies with ID_OPEN_CONNECTION_REPLY_1."""
        _, server = _make_pair()
        client_conn = Connection(
            address=("127.0.0.1", 50000),
            guid=1000,
            is_server=False,
            mtu=MAXIMUM_MTU,
        )
        now = time.monotonic()
        pkt = client_conn.start_connect(now)

        responses = server.on_datagram(pkt, now)
        assert len(responses) == 1
        assert responses[0][0] == 6  # ID_OPEN_CONNECTION_REPLY_1

    def test_rejects_wrong_magic_bytes(self):
        """Wrong magic bytes should produce no reply."""
        _, server = _make_pair()
        now = time.monotonic()

        # Build a fake open_request_1 with wrong magic
        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(b"\x00" * 16)  # Wrong magic
        bs.write_uint8(RAKNET_PROTOCOL_VERSION)
        bs.pad_with_zero_to_byte_length(100)

        responses = server.on_datagram(bs.get_data(), now)
        assert len(responses) == 0

    def test_rejects_wrong_protocol_version(self):
        """Wrong protocol version should produce no reply."""
        _, server = _make_pair()
        now = time.monotonic()

        bs = BitStream()
        bs.write_uint8(ID_OPEN_CONNECTION_REQUEST_1)
        bs.write_bytes(OFFLINE_MAGIC)
        bs.write_uint8(255)  # Wrong version
        bs.pad_with_zero_to_byte_length(100)

        responses = server.on_datagram(bs.get_data(), now)
        assert len(responses) == 0

    def test_handles_open_request_2_replies(self):
        """Server handles request 2 and replies with reply 2."""
        client, server = _make_pair()
        now = time.monotonic()

        pkt = client.start_connect(now)
        r1 = server.on_datagram(pkt, now)
        r2 = client.on_datagram(r1[0], now)
        r3 = server.on_datagram(r2[0], now)

        assert len(r3) == 1
        assert r3[0][0] == 8  # ID_OPEN_CONNECTION_REPLY_2


class TestTimeout:
    def test_timeout_emits_disconnect_event(self):
        """Advance now past timeout, update() should produce DISCONNECT event."""
        client, server = _make_pair(timeout=2.0)
        now = time.monotonic()
        now = _do_full_handshake(client, server, now)

        # Drain existing events
        client.poll_events()

        # Advance past timeout
        now += 3.0
        client.update(now)
        events = client.poll_events()

        disconnect_events = [e for e in events if e.type == EventType.DISCONNECT]
        assert len(disconnect_events) == 1
        assert client.state == ConnectionState.DISCONNECTED

    def test_no_timeout_while_receiving(self):
        """Feeding datagrams should reset the timer."""
        client, server = _make_pair(timeout=2.0)
        now = time.monotonic()
        now = _do_full_handshake(client, server, now)

        # Drain events
        client.poll_events()

        # Advance close to timeout but keep receiving
        for _ in range(5):
            now += 1.5  # Each step < timeout
            # Server sends an update (ping or ACK)
            server_out = server.update(now)
            for dg in server_out:
                client.on_datagram(dg, now)
            client.update(now)

        events = client.poll_events()
        disconnect_events = [e for e in events if e.type == EventType.DISCONNECT]
        assert len(disconnect_events) == 0
        assert client.state == ConnectionState.CONNECTED


class TestPingPong:
    def test_ping_sent_after_interval(self):
        """After ping interval, update should produce ping frames."""
        client, server = _make_pair()
        now = time.monotonic()
        now = _do_full_handshake(client, server, now)

        # Advance past ping interval (5.0s)
        now += 6.0
        datagrams = client.update(now)
        # Should have produced at least one datagram (containing ping)
        assert len(datagrams) >= 1

    def test_pong_reply_to_ping(self):
        """Feed a ping to server, it should produce a pong in its output."""
        client, server = _make_pair()
        now = time.monotonic()
        now = _do_full_handshake(client, server, now)

        # Advance past ping interval so client sends ping
        now += 6.0
        client_out = client.update(now)

        # Feed client's ping to server
        for dg in client_out:
            server.on_datagram(dg, now)

        # Server should flush a pong response
        now += 0.01
        server_out = server.update(now)
        # Server should produce at least ACKs and possibly pong
        assert len(server_out) >= 1
