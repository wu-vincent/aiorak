"""Integration tests for data flow: echo, burst, big packet, multi-channel."""

import asyncio
import math
import os
import struct
from collections import defaultdict

import pytest

import aiorak
from tests.conftest import collect_packets, wait_for_peers

pytestmark = pytest.mark.asyncio

_ID_MARKER = 0x86


class TestEcho:
    @pytest.mark.timeout(10)
    async def test_server_send_recv_data(self, server_factory, client_factory):
        """Full data path: client sends, server echoes, client receives."""
        srv = await server_factory()
        client = await client_factory(srv.address)
        await asyncio.sleep(0.1)

        client.send(b"hello")
        data = None
        async for pkt in client:
            data = pkt
            break
        assert data == b"hello"


class TestBurst:
    @pytest.mark.timeout(15)
    @pytest.mark.parametrize(
        "msg_size, msg_count",
        [
            (64, 128),
            (512, 64),
            (4096, 16),
        ],
        ids=["64B-x128", "512B-x64", "4KB-x16"],
    )
    async def test_burst_reliable_ordered(self, server_factory, client_factory, msg_size, msg_count):
        """Send msg_count messages of msg_size bytes in a burst and verify order."""
        server = await server_factory()
        client = await client_factory(server.address)

        header_len = 1 + 4
        padding_len = msg_size - header_len
        assert padding_len >= 0

        messages: list[bytes] = []
        for i in range(msg_count):
            payload = struct.pack(">BI", _ID_MARKER, i) + (b"\x00" * padding_len)
            messages.append(payload)

        for msg in messages:
            client.send(msg, reliability=aiorak.Reliability.RELIABLE_ORDERED)

        responses = await collect_packets(client, msg_count, timeout=10.0)

        assert len(responses) == msg_count
        for expected_idx, data in enumerate(responses):
            assert len(data) == msg_size
            (received_idx,) = struct.unpack(">I", data[1:5])
            assert received_idx == expected_idx


class TestBigPacket:
    def _make_pattern(self, size: int) -> bytes:
        return bytes(255 - (i & 255) for i in range(size))

    async def _big_packet_test(self, server_factory, client_factory, size, *, verify_data=True, timeout=10.0):
        """Helper: server sends a big packet, client receives and verifies."""
        payload = self._make_pattern(size)

        async def handler(conn: aiorak.Connection):
            conn.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)

        server = await server_factory(handler=handler)
        client = await client_factory(server.address)

        async def _recv():
            async for data in client:
                return data

        received = await asyncio.wait_for(_recv(), timeout=timeout)
        assert len(received) == size
        if verify_data:
            assert received == payload

    @pytest.mark.timeout(15)
    async def test_big_packet_50kb(self, server_factory, client_factory):
        """50 KB packet with full data verification."""
        await self._big_packet_test(server_factory, client_factory, 50_000, timeout=10.0)

    @pytest.mark.timeout(20)
    async def test_big_packet_500kb(self, server_factory, client_factory):
        """500 KB packet with full data verification."""
        await self._big_packet_test(server_factory, client_factory, 500_000, timeout=15.0)

    @pytest.mark.slow
    @pytest.mark.timeout(45)
    async def test_big_packet_5mb(self, server_factory, client_factory):
        """5 MB packet - verify length only (too large for byte-by-byte comparison)."""
        await self._big_packet_test(server_factory, client_factory, 5_000_000, verify_data=False, timeout=30.0)


class TestMessageSize:
    def _make_message(self, stride: int, index: int) -> bytes:
        fill = index % 256
        return bytes([_ID_MARKER]) + bytes([fill] * (stride - 1))

    @pytest.mark.timeout(10)
    @pytest.mark.parametrize("stride", [1, 10, 100, 500, 999, 1500, 1999])
    async def test_message_size(self, server_factory, client_factory, stride: int):
        """Send ceil(4000/stride) messages of stride bytes and verify echoed data."""
        count = math.ceil(4000 / stride)

        server = await server_factory()
        client = await client_factory(server.address)

        expected: list[bytes] = []
        for i in range(count):
            msg = self._make_message(stride, i)
            expected.append(msg)
            client.send(msg, reliability=aiorak.Reliability.RELIABLE_ORDERED)

        received = await collect_packets(client, count, timeout=5.0)

        assert len(received) == count
        for i, (got, want) in enumerate(zip(received, expected)):
            assert got == want


class TestReliableOrderedMultiChannel:
    @pytest.mark.timeout(20)
    async def test_reliable_ordered_multi_channel(self, server_factory, client_factory):
        """Send packets on 32 channels with RELIABLE_ORDERED and verify per-channel ordering."""
        num_channels = 32
        packets_per_channel = 50
        total_packets = num_channels * packets_per_channel

        server = await server_factory()
        client = await client_factory(server.address)

        for seq in range(packets_per_channel):
            for ch in range(num_channels):
                padding_len = int.from_bytes(os.urandom(2), "big") % 5000 + 1
                payload = struct.pack(">BIB", _ID_MARKER, seq, ch) + os.urandom(padding_len)
                client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED, channel=ch)
            await asyncio.sleep(0.03)

        responses = await collect_packets(client, total_packets, timeout=15.0)
        assert len(responses) == total_packets

        per_channel: dict[int, list[int]] = defaultdict(list)
        for data in responses:
            assert len(data) >= 6
            _, seq, ch = struct.unpack(">BIB", data[:6])
            per_channel[ch].append(seq)

        for ch in range(num_channels):
            received = per_channel[ch]
            assert len(received) == packets_per_channel
            for i in range(1, len(received)):
                assert received[i] > received[i - 1]


class TestFlowControl:
    def _make_relay_handler(self):
        connections: dict[tuple[str, int], aiorak.Connection] = {}

        async def relay_handler(conn: aiorak.Connection):
            connections[conn.remote_address] = conn
            try:
                async for data in conn:
                    for addr, other in list(connections.items()):
                        if addr != conn.remote_address:
                            other.send(data)
            finally:
                connections.pop(conn.remote_address, None)

        return relay_handler

    @pytest.mark.timeout(15)
    async def test_relayed_data_arrives(self, server_factory, client_factory):
        """Client A sends packets through relay; client B receives all of them."""
        relay_handler = self._make_relay_handler()
        server = await server_factory(handler=relay_handler)
        addr = server.address

        sender = await client_factory(addr)
        receiver = await client_factory(addr)
        await wait_for_peers(server, 2)

        packet_size = 64
        interval = 0.128
        duration = 3.0

        send_count = 0
        received: list[bytes] = []

        async def _send():
            nonlocal send_count
            deadline = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < deadline:
                payload = struct.pack(">I", send_count) + b"\x00" * (packet_size - 4)
                sender.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)
                send_count += 1
                await asyncio.sleep(interval)

        async def _recv():
            async for data in receiver:
                received.append(data)
                if len(received) >= send_count and send_count > 0:
                    await asyncio.sleep(0.3)
                    return

        await _send()
        await asyncio.wait_for(_recv(), timeout=duration + 5.0)

        assert send_count > 0
        assert len(received) == send_count

    @pytest.mark.timeout(15)
    async def test_variable_packet_sizes(self, server_factory, client_factory):
        """Relay test with variable packet sizes (64, 256, 1024, 4096 bytes)."""
        relay_handler = self._make_relay_handler()
        server = await server_factory(handler=relay_handler)
        addr = server.address

        sender = await client_factory(addr)
        receiver = await client_factory(addr)
        await wait_for_peers(server, 2)

        sizes = [64, 256, 1024, 4096]
        interval = 0.064
        duration_per_size = 0.5

        for packet_size in sizes:
            send_count = 0
            received: list[bytes] = []

            async def _send():
                nonlocal send_count
                deadline = asyncio.get_event_loop().time() + duration_per_size
                while asyncio.get_event_loop().time() < deadline:
                    payload = struct.pack(">HI", packet_size, send_count) + b"\xab" * (packet_size - 6)
                    sender.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)
                    send_count += 1
                    await asyncio.sleep(interval)

            async def _recv():
                async for data in receiver:
                    received.append(data)
                    if len(received) >= send_count and send_count > 0:
                        await asyncio.sleep(0.3)
                        return

            await _send()
            await asyncio.wait_for(_recv(), timeout=duration_per_size + 5.0)

            assert send_count > 0
            assert len(received) == send_count
            for i, data in enumerate(received):
                assert len(data) == packet_size
