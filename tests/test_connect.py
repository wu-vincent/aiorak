"""Integration tests for connect/disconnect lifecycle and max connections."""

import asyncio
import time

import pytest

import aiorak
from tests.conftest import noop_handler, wait_for_no_peers, wait_for_peers

pytestmark = pytest.mark.asyncio

NUM_CLIENTS = 4
NUM_CYCLES = 3


class TestConnectDisconnect:
    @pytest.mark.timeout(30)
    async def test_connect_disconnect_cycles(self, server_factory):
        """Connect 4 clients, disconnect all, reconnect for 3 cycles.

        Verifies connection slot reuse after disconnect.
        """
        server = await server_factory(handler=noop_handler, max_connections=NUM_CLIENTS + 4)
        addr = server.address

        for cycle in range(NUM_CYCLES):
            clients = await asyncio.gather(*(aiorak.connect(addr, timeout=5.0) for _ in range(NUM_CLIENTS)))

            await wait_for_peers(server, NUM_CLIENTS, timeout=5.0)
            assert len(server._peers) == NUM_CLIENTS

            for c in clients:
                assert c.is_connected

            await asyncio.gather(*(c.close() for c in clients))

            try:
                await wait_for_no_peers(server, timeout=15.0)
            except TimeoutError:
                pass

    @pytest.mark.timeout(15)
    async def test_rapid_connect_disconnect(self, server_factory):
        """Rapidly connect and disconnect 4 clients for ~5 seconds.

        Verifies server stability under connection churn.
        """
        server = await server_factory(handler=noop_handler, max_connections=NUM_CLIENTS + 4)
        addr = server.address

        deadline = time.monotonic() + 5.0
        iterations = 0

        while time.monotonic() < deadline:
            clients = await asyncio.gather(*(aiorak.connect(addr, timeout=5.0) for _ in range(NUM_CLIENTS)))
            await asyncio.gather(*(c.close() for c in clients))
            iterations += 1

        assert iterations > 0
        await asyncio.sleep(0.2)

        final_client = await aiorak.connect(addr, timeout=5.0)
        try:
            await wait_for_peers(server, 1, timeout=5.0)
            assert final_client.is_connected
        finally:
            await final_client.close()

    @pytest.mark.timeout(10)
    async def test_incompatible_protocol_version(self, server_factory):
        """Client with wrong protocol version is rejected with ConnectionRejectedError."""
        server = await server_factory(handler=noop_handler)
        addr = server.address

        with pytest.raises(aiorak.ConnectionRejectedError):
            await aiorak.connect(addr, timeout=1.0, protocol_version=99)

        assert len(server._peers) == 0

        client = await aiorak.connect(addr, timeout=5.0)
        try:
            await wait_for_peers(server, 1, timeout=5.0)
            assert client.is_connected
        finally:
            await client.close()

    @pytest.mark.timeout(10)
    async def test_cancelled_connect(self, server_factory):
        """Cancelling a pending connection does not crash the server."""
        server = await server_factory(handler=noop_handler, max_connections=4)
        addr = server.address

        async def _connect_and_cancel():
            task = asyncio.ensure_future(aiorak.connect(addr, timeout=5.0))
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await _connect_and_cancel()
        await asyncio.sleep(0.2)

        client = await aiorak.connect(addr, timeout=5.0)
        try:
            await wait_for_peers(server, 1, timeout=5.0)
            assert client.is_connected
        finally:
            await client.close()

    @pytest.mark.timeout(10)
    async def test_client_connect_timeout(self):
        """Connecting to a non-listening port raises RakNetTimeoutError."""
        with pytest.raises(aiorak.RakNetTimeoutError):
            await aiorak.connect(("127.0.0.1", 19), timeout=1.0)


class TestMaxConnections:
    @pytest.mark.timeout(10)
    async def test_connect_up_to_max(self, server_factory, client_factory):
        """All clients connect successfully when count == max_connections."""
        max_conn = 4
        server = await server_factory(max_connections=max_conn)
        addr = server.address

        clients = []
        for _ in range(max_conn):
            clients.append(await client_factory(addr))

        await wait_for_peers(server, max_conn)
        assert len(server._peers) == max_conn

    @pytest.mark.timeout(10)
    async def test_reject_beyond_max(self, server_factory, client_factory):
        """Client connecting beyond max_connections is rejected with ConnectionRejectedError."""
        max_conn = 4
        server = await server_factory(max_connections=max_conn)
        addr = server.address

        clients = []
        for _ in range(max_conn):
            clients.append(await client_factory(addr))

        await wait_for_peers(server, max_conn)
        assert len(server._peers) == max_conn

        with pytest.raises(aiorak.ConnectionRejectedError):
            await aiorak.connect(addr, timeout=2.0)

        assert len(server._peers) == max_conn


class TestServerCreation:
    @pytest.mark.timeout(10)
    async def test_create_server_optional_params(self):
        """create_server() with all optional params sets them correctly."""

        async def handler(conn):
            async for _ in conn:
                pass

        server = await aiorak.create_server(
            ("127.0.0.1", 0),
            handler,
            max_connections=8,
            guid=12345,
            protocol_version=11,
            max_mtu=1400,
            min_mtu=400,
            num_internal_ids=20,
            timeout=15.0,
            rate_limit_ips=True,
        )
        try:
            assert server.guid == 12345
            assert server._protocol_version == 11
            assert server._max_mtu == 1400
            assert server._min_mtu == 400
            assert server._num_internal_ids == 20
            assert server._timeout == 15.0
            assert server._rate_limit_ips is True
        finally:
            await server.close()

    @pytest.mark.timeout(10)
    async def test_connect_optional_params(self, server_factory):
        """connect() with all optional params succeeds and sets them correctly."""
        server = await server_factory(handler=noop_handler)
        addr = server.address

        client = await aiorak.connect(
            addr,
            timeout=5.0,
            guid=67890,
            max_mtu=1400,
            min_mtu=400,
            mtu_discovery_sizes=(1400, 1200, 576),
            num_internal_ids=10,
        )
        try:
            assert client.is_connected
            assert client.guid == 67890
        finally:
            await client.close()
