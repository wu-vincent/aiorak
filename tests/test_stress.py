"""Stress tests: many clients, throughput, cross-connection, dropped connections."""

import asyncio
import os
import random
import struct
import sys
import time
from collections import defaultdict

import pytest

import aiorak
from tests.conftest import collect_packets, force_close_transport, noop_handler, wait_for_peers

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


async def _connect_all(addr, count, timeout=5.0):
    return list(await asyncio.gather(*(aiorak.connect(addr, timeout=timeout) for _ in range(count))))


async def _close_all(clients):
    await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)


class TestManyClients:
    @pytest.mark.timeout(15)
    async def test_initial_connect_32(self, server_factory):
        """32 clients connect to a server and all are accepted."""
        server = await server_factory(handler=noop_handler, max_connections=42)
        clients = await _connect_all(server.address, 32)
        try:
            await wait_for_peers(server, 32, timeout=10.0)
            assert len(server._peers) == 32
        finally:
            await _close_all(clients)

    @pytest.mark.timeout(90)
    async def test_disconnect_reconnect_cycle(self, server_factory):
        """32 clients connect, then go through 2 disconnect/reconnect cycles."""
        server = await server_factory(handler=noop_handler, max_connections=128)
        clients = await _connect_all(server.address, 32)
        await wait_for_peers(server, 32, timeout=10.0)

        for cycle in range(2):
            await _close_all(clients)
            await asyncio.sleep(0.5)
            clients = await _connect_all(server.address, 32)
            await wait_for_peers(server, 32, timeout=10.0)
            assert len(server._peers) >= 32

        await _close_all(clients)

    @pytest.mark.timeout(30)
    async def test_abrupt_disconnect(self, server_factory):
        """32 clients connect then have transports forcefully closed; server recovers."""
        server = await server_factory(handler=noop_handler, max_connections=128)
        _disable_udp_connreset(server)
        clients = await _connect_all(server.address, 32)
        await wait_for_peers(server, 32, timeout=10.0)

        for c in clients:
            c._socket._transport.close()

        deadline = time.monotonic() + 15.0
        while (len(server._peers) > 0 or len(server._connections) > 0) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        assert len(server._peers) == 0

        clients = await _connect_all(server.address, 32)
        try:
            await wait_for_peers(server, 32, timeout=10.0)
            assert len(server._peers) == 32
        finally:
            await _close_all(clients)

    @pytest.mark.timeout(15)
    async def test_rapid_churn(self, server_factory):
        """16 clients rapidly close and reconnect for 3 seconds."""
        server = await server_factory(handler=noop_handler, max_connections=26)
        clients = await _connect_all(server.address, 16)
        await wait_for_peers(server, 16, timeout=10.0)

        end_time = time.monotonic() + 3.0
        while time.monotonic() < end_time:
            await _close_all(clients)
            await asyncio.sleep(0.05)
            clients = await _connect_all(server.address, 16, timeout=5.0)
            await asyncio.sleep(0.05)

        await _close_all(clients)
        deadline = time.monotonic() + 5.0
        while len(server._peers) > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        clients = await _connect_all(server.address, 16)
        try:
            await wait_for_peers(server, 16, timeout=10.0)
            assert len(server._peers) == 16
        finally:
            await _close_all(clients)


class TestAllClientsConnect:
    @pytest.mark.timeout(15)
    async def test_all_clients_connect_100(self, server_factory):
        """100 clients connect to a single server and all are accepted."""
        server = await server_factory(handler=noop_handler, max_connections=110)
        clients = await asyncio.gather(*(aiorak.connect(server.address, timeout=10.0) for _ in range(100)))
        try:
            await wait_for_peers(server, 100, timeout=10.0)
            assert len(server._peers) == 100
        finally:
            await asyncio.gather(*(c.close() for c in clients))


class TestBidirectionalData:
    @pytest.mark.timeout(10)
    async def test_bidirectional_data_flow(self, server_factory):
        """10 clients each send 10 messages and receive echoed responses in order."""
        server = await server_factory(max_connections=20)
        clients = await asyncio.gather(*(aiorak.connect(server.address, timeout=5.0) for _ in range(10)))

        try:
            await wait_for_peers(server, 10, timeout=5.0)

            async def _client_roundtrip(client, client_id):
                received = []
                for i in range(10):
                    payload = f"client{client_id}-msg{i}".encode()
                    client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)
                async for data in client:
                    received.append(data)
                    if len(received) == 10:
                        break
                for i, data in enumerate(received):
                    expected = f"client{client_id}-msg{i}".encode()
                    assert data == expected

            await asyncio.gather(*(_client_roundtrip(c, idx) for idx, c in enumerate(clients)))
            assert len(server._peers) == 10
        finally:
            await asyncio.gather(*(c.close() for c in clients))


class TestDroppedConnection:
    @pytest.mark.timeout(15)
    async def test_server_detects_client_gone(self, server_factory, client_factory):
        """Server detects silently closed clients via timeout."""
        server = await server_factory(max_connections=5)
        server.timeout = 2.0
        clients = []
        for _ in range(5):
            clients.append(await client_factory(server.address))

        await wait_for_peers(server, 5)

        for i in range(3):
            await clients[i].close(notify=False)

        async def _wait_for_drop():
            while len(server._peers) > 2:
                await asyncio.sleep(0.2)

        await asyncio.wait_for(_wait_for_drop(), timeout=10.0)
        assert len(server._peers) == 2

    @pytest.mark.timeout(15)
    async def test_client_detects_server_gone(self, server_factory, client_factory):
        """Client detects silently closed server and ends async iteration."""
        server = await server_factory()
        client = await client_factory(server.address)
        client.timeout = 2.0
        await wait_for_peers(server, 1)

        await server.close(notify=False)

        async def _drain_client():
            async for _data in client:
                pass

        await asyncio.wait_for(_drain_client(), timeout=10.0)
        assert not client.is_connected

    @pytest.mark.timeout(15)
    async def test_random_disconnect_reconnect(self, server_factory, client_factory):
        """Randomly disconnect and reconnect clients for 5 seconds without crashing."""
        server = await server_factory(max_connections=7)
        clients: list[aiorak.Client | None] = []
        for _ in range(5):
            cli = await client_factory(server.address)
            clients.append(cli)

        await wait_for_peers(server, 5)

        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            idx = random.randint(0, 4)
            cli = clients[idx]
            if cli is not None and cli.is_connected:
                if random.random() < 0.5:
                    await cli.close(notify=True)
                else:
                    await cli.close(notify=False)
                clients[idx] = None
            elif cli is None:
                try:
                    new_cli = await aiorak.connect(server.address, timeout=3.0)
                    clients[idx] = new_cli
                except aiorak.RakNetError:
                    pass
            await asyncio.sleep(random.uniform(0.05, 0.3))

        for cli in clients:
            if cli is not None:
                try:
                    await cli.close()
                except Exception:
                    pass


class TestCrossConnection:
    @pytest.mark.timeout(10)
    async def test_simultaneous_connect(self, server_factory):
        """Two servers on different ports; clients connect cross-wise simultaneously."""
        server_a = await server_factory(handler=noop_handler, max_connections=4)
        server_b = await server_factory(handler=noop_handler, max_connections=4)

        client_a, client_b = await asyncio.gather(
            aiorak.connect(server_b.address, timeout=5.0),
            aiorak.connect(server_a.address, timeout=5.0),
        )

        try:
            await wait_for_peers(server_a, 1, timeout=5.0)
            await wait_for_peers(server_b, 1, timeout=5.0)
            assert client_a.is_connected
            assert client_b.is_connected
        finally:
            await asyncio.gather(client_a.close(), client_b.close())

    @pytest.mark.timeout(30)
    async def test_cross_connect_disconnect_cycle(self, server_factory):
        """Cross-connection with data exchange for 3 cycles."""
        received_a = []
        received_b = []

        async def _handler_a(conn):
            async for data in conn:
                received_a.append(data)
                conn.send(data)

        async def _handler_b(conn):
            async for data in conn:
                received_b.append(data)
                conn.send(data)

        server_a = await server_factory(handler=_handler_a, max_connections=4)
        server_b = await server_factory(handler=_handler_b, max_connections=4)

        for cycle in range(3):
            received_a.clear()
            received_b.clear()

            client_a, client_b = await asyncio.gather(
                aiorak.connect(server_b.address, timeout=5.0),
                aiorak.connect(server_a.address, timeout=5.0),
            )

            try:
                await wait_for_peers(server_a, 1, timeout=5.0)
                await wait_for_peers(server_b, 1, timeout=5.0)

                msg_a = f"hello from A cycle {cycle}".encode()
                msg_b = f"hello from B cycle {cycle}".encode()

                client_a.send(msg_a, reliability=aiorak.Reliability.RELIABLE_ORDERED)
                client_b.send(msg_b, reliability=aiorak.Reliability.RELIABLE_ORDERED)

                async def _recv(client):
                    async for data in client:
                        return data

                echo_a, echo_b = await asyncio.gather(
                    asyncio.wait_for(_recv(client_a), timeout=3.0),
                    asyncio.wait_for(_recv(client_b), timeout=3.0),
                )

                assert echo_a == msg_a
                assert echo_b == msg_b
            finally:
                await asyncio.gather(client_a.close(), client_b.close())
            await asyncio.sleep(0.2)


class TestEightPeerBroadcast:
    @pytest.mark.timeout(60)
    async def test_eight_peer_broadcast(self, server_factory, client_factory):
        """Eight clients exchange 100 reliable-ordered messages each via broadcast."""
        num_clients = 8
        messages_per_client = 100
        expected_per_client = messages_per_client * (num_clients - 1)
        padding = b"\xab" * 200

        clients_map: dict[tuple[str, int], aiorak.Connection] = {}

        async def broadcast_handler(conn):
            clients_map[conn.remote_address] = conn
            try:
                async for data in conn:
                    for addr, other in list(clients_map.items()):
                        if addr != conn.remote_address:
                            other.send(data)
            finally:
                clients_map.pop(conn.remote_address, None)

        server = await server_factory(handler=broadcast_handler, max_connections=10)
        peers = []
        for _ in range(num_clients):
            cli = await client_factory(server.address, timeout=10.0)
            peers.append(cli)

        await wait_for_peers(server, num_clients, timeout=15.0)

        received: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        recv_counts = [0] * num_clients

        async def receiver(client_idx, client):
            async for data in client:
                sender_id = data[0]
                (seq,) = struct.unpack_from(">I", data, 1)
                received[client_idx][sender_id].append(seq)
                recv_counts[client_idx] += 1
                if recv_counts[client_idx] >= expected_per_client:
                    break

        async def sender(client_idx, client):
            for seq in range(messages_per_client):
                payload = bytes([client_idx]) + struct.pack(">I", seq) + padding
                client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED, channel=0)
                await asyncio.sleep(0.01)

        tasks = []
        for idx, cli in enumerate(peers):
            tasks.append(asyncio.create_task(receiver(idx, cli)))
            tasks.append(asyncio.create_task(sender(idx, cli)))

        done, pending = await asyncio.wait(tasks, timeout=60.0)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc

        total_received = sum(recv_counts)
        expected_total = num_clients * expected_per_client
        assert total_received == expected_total


class TestThroughput:
    async def _send_packets(self, client, reliability, count=500, size=400, rate=100, duration=3.0):
        interval = 1.0 / rate
        sent = 0
        deadline = asyncio.get_event_loop().time() + duration
        while sent < count and asyncio.get_event_loop().time() < deadline:
            payload = struct.pack(">I", sent) + b"\xcc" * (size - 4)
            client.send(payload, reliability=reliability)
            sent += 1
            await asyncio.sleep(interval)
        return sent

    @pytest.mark.timeout(20)
    async def test_throughput_reliable_ordered(self, server_factory, client_factory):
        """500 packets of 400 bytes with RELIABLE_ORDERED: all echoed back in order."""
        server = await server_factory()
        client = await client_factory(server.address)

        sent = await self._send_packets(client, aiorak.Reliability.RELIABLE_ORDERED)
        responses = await collect_packets(client, sent, timeout=13.0)
        assert len(responses) == sent

        for i, data in enumerate(responses):
            assert len(data) == 400
            (idx,) = struct.unpack(">I", data[:4])
            assert idx == i

    @pytest.mark.timeout(20)
    async def test_throughput_reliable(self, server_factory, client_factory):
        """500 packets of 400 bytes with RELIABLE: all echoed back."""
        server = await server_factory()
        client = await client_factory(server.address)

        sent = await self._send_packets(client, aiorak.Reliability.RELIABLE)
        responses = await collect_packets(client, sent, timeout=13.0)
        assert len(responses) == sent

    @pytest.mark.timeout(20)
    async def test_throughput_unreliable(self, server_factory, client_factory):
        """500 packets of 400 bytes with UNRELIABLE: at least 50% arrive."""
        server = await server_factory()
        client = await client_factory(server.address)

        sent = await self._send_packets(client, aiorak.Reliability.UNRELIABLE)

        received = []

        async def _gather():
            async for data in client:
                received.append(data)
                if len(received) >= sent:
                    return

        try:
            await asyncio.wait_for(_gather(), timeout=8.0)
        except TimeoutError:
            pass

        assert len(received) >= sent // 2


class TestComprehensiveStress:
    @pytest.mark.timeout(20)
    async def test_comprehensive_stress(self, server_factory):
        """Randomly connect, send, disconnect, and ping for 5 seconds.

        Stability test: passes as long as no unhandled exceptions bubble up.
        """
        num_slots = 10
        server = await server_factory(max_connections=num_slots)
        addr = server.address

        clients: list[aiorak.Client | None] = [None] * num_slots
        drain_tasks: list[asyncio.Task | None] = [None] * num_slots

        async def _drain(client):
            try:
                async for _data in client:
                    pass
            except Exception:
                pass

        deadline = asyncio.get_event_loop().time() + 5.0
        errors = []

        while asyncio.get_event_loop().time() < deadline:
            roll = random.random()
            try:
                if roll < 0.10:
                    disconnected = [i for i in range(num_slots) if clients[i] is None]
                    if disconnected:
                        idx = random.choice(disconnected)
                        try:
                            cli = await aiorak.connect(addr, timeout=3.0)
                            clients[idx] = cli
                            drain_tasks[idx] = asyncio.create_task(_drain(cli))
                        except aiorak.RakNetError:
                            pass
                elif roll < 0.30:
                    connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                    if connected:
                        idx = random.choice(connected)
                        try:
                            clients[idx].send(
                                os.urandom(random.randint(1, 512)), reliability=aiorak.Reliability.RELIABLE_ORDERED
                            )
                        except Exception:
                            pass
                elif roll < 0.40:
                    connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                    if connected:
                        idx = random.choice(connected)
                        try:
                            await clients[idx].close()
                        except Exception:
                            pass
                        if drain_tasks[idx] is not None:
                            drain_tasks[idx].cancel()
                            drain_tasks[idx] = None
                        clients[idx] = None
                elif roll < 0.45:
                    connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                    if connected:
                        idx = random.choice(connected)
                        try:
                            force_close_transport(clients[idx])
                        except Exception:
                            pass
                        if drain_tasks[idx] is not None:
                            drain_tasks[idx].cancel()
                            drain_tasks[idx] = None
                        clients[idx] = None
                elif roll < 0.50:
                    try:
                        await aiorak.ping(addr, timeout=1.0)
                    except aiorak.RakNetTimeoutError:
                        pass
                else:
                    await asyncio.sleep(random.uniform(0.01, 0.05))
                    continue
            except Exception as exc:
                errors.append(exc)

            await asyncio.sleep(0.01)

        for i in range(num_slots):
            if drain_tasks[i] is not None:
                drain_tasks[i].cancel()
            if clients[i] is not None:
                try:
                    await clients[i].close()
                except Exception:
                    pass

        await asyncio.sleep(0.1)
        assert not errors


def _disable_udp_connreset(server):
    """Prevent ICMP Port Unreachable errors from killing the server socket on Windows."""
    if sys.platform != "win32":
        return
    import ctypes

    sock = server._socket._transport.get_extra_info("socket")
    if sock is None:
        return
    SIO_UDP_CONNRESET = 0x9800000C
    ret = ctypes.c_ulong(0)
    ctypes.windll.ws2_32.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(ctypes.c_bool(False)),
        ctypes.sizeof(ctypes.c_bool),
        None,
        0,
        ctypes.byref(ret),
        None,
        None,
    )
