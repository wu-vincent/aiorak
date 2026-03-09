"""Port of CrossConnectionTest.cpp — simultaneous connections between two servers.

Since aiorak uses a client-server model (not peer-to-peer), the test creates
two servers and connects a client from each side to the other server at the
same time using asyncio.gather.
"""

import asyncio

import pytest

import aiorak


async def wait_for_peers(server, count, timeout=5.0):
    """Wait until server has at least count connected peers."""

    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


pytestmark = pytest.mark.asyncio

NUM_CYCLES = 3


@pytest.mark.timeout(10)
async def test_simultaneous_connect(server_factory):
    """Two servers on different ports; clients connect cross-wise simultaneously.

    Client A connects to Server B while Client B connects to Server A at
    the same time. Both connections should succeed.
    """

    async def _noop_handler(conn):
        async for _ in conn:
            pass

    server_a = await server_factory(handler=_noop_handler, max_connections=4)
    server_b = await server_factory(handler=_noop_handler, max_connections=4)
    addr_a = server_a.local_address
    addr_b = server_b.local_address

    # Connect both directions simultaneously.
    client_a, client_b = await asyncio.gather(
        aiorak.connect(addr_b, timeout=5.0),
        aiorak.connect(addr_a, timeout=5.0),
    )

    try:
        # Wait for both servers to register their peers.
        await wait_for_peers(server_a, 1, timeout=5.0)
        await wait_for_peers(server_b, 1, timeout=5.0)

        assert client_a.is_connected
        assert client_b.is_connected
        assert len(server_a._peers) == 1
        assert len(server_b._peers) == 1
    finally:
        await asyncio.gather(client_a.close(), client_b.close())


@pytest.mark.timeout(30)
async def test_cross_connect_disconnect_cycle(server_factory):
    """Repeat cross-connection with data exchange for 3 cycles.

    Each cycle: connect both directions, send data both ways to verify the
    connections are functional, then disconnect both clients.
    """

    received_a: list[bytes] = []
    received_b: list[bytes] = []

    async def _handler_a(conn):
        async for data in conn:
            received_a.append(data)
            await conn.send(data)

    async def _handler_b(conn):
        async for data in conn:
            received_b.append(data)
            await conn.send(data)

    server_a = await server_factory(handler=_handler_a, max_connections=4)
    server_b = await server_factory(handler=_handler_b, max_connections=4)
    addr_a = server_a.local_address
    addr_b = server_b.local_address

    for cycle in range(NUM_CYCLES):
        received_a.clear()
        received_b.clear()

        # Connect cross-wise simultaneously.
        client_a, client_b = await asyncio.gather(
            aiorak.connect(addr_b, timeout=5.0),
            aiorak.connect(addr_a, timeout=5.0),
        )

        try:
            await wait_for_peers(server_a, 1, timeout=5.0)
            await wait_for_peers(server_b, 1, timeout=5.0)

            # Send data in both directions.
            msg_a = f"hello from A cycle {cycle}".encode()
            msg_b = f"hello from B cycle {cycle}".encode()

            await client_a.send(msg_a, reliability=aiorak.Reliability.RELIABLE_ORDERED)
            await client_b.send(msg_b, reliability=aiorak.Reliability.RELIABLE_ORDERED)

            # Receive echoed responses.
            echo_a = await asyncio.wait_for(client_a.recv(), timeout=3.0)
            echo_b = await asyncio.wait_for(client_b.recv(), timeout=3.0)

            assert echo_a == msg_a, f"Cycle {cycle}: client A expected {msg_a!r}, got {echo_a!r}"
            assert echo_b == msg_b, f"Cycle {cycle}: client B expected {msg_b!r}, got {echo_b!r}"

            # Verify the servers received the data.
            assert msg_a in received_b, f"Cycle {cycle}: server B did not receive message from client A"
            assert msg_b in received_a, f"Cycle {cycle}: server A did not receive message from client B"
        finally:
            await asyncio.gather(client_a.close(), client_b.close())

        # Wait for servers to deregister the peers before next cycle.
        await asyncio.sleep(0.2)
