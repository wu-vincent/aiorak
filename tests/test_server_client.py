"""Port of ServerClientTest.cpp — many clients connecting with bidirectional data flow."""

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

NUM_CLIENTS_CONNECT = 100
NUM_CLIENTS_DATA = 10
NUM_MESSAGES = 10


@pytest.mark.slow
@pytest.mark.timeout(15)
async def test_all_clients_connect(server_factory):
    """100 clients connect to a single server and all are accepted."""

    async def _noop_handler(conn):
        async for _ in conn:
            pass

    server = await server_factory(handler=_noop_handler, max_connections=NUM_CLIENTS_CONNECT + 10)
    addr = server.local_address

    # Connect all clients in parallel.
    clients = await asyncio.gather(*(aiorak.connect(addr, timeout=10.0) for _ in range(NUM_CLIENTS_CONNECT)))

    try:
        await wait_for_peers(server, NUM_CLIENTS_CONNECT, timeout=10.0)
        assert len(server._peers) == NUM_CLIENTS_CONNECT
    finally:
        await asyncio.gather(*(c.close() for c in clients))


@pytest.mark.timeout(10)
async def test_bidirectional_data_flow(server_factory):
    """10 clients each send 10 messages and receive echoed responses.

    After the data exchange, verify that no connections were lost.
    """
    server = await server_factory(max_connections=NUM_CLIENTS_DATA + 10)
    addr = server.local_address

    clients = await asyncio.gather(*(aiorak.connect(addr, timeout=5.0) for _ in range(NUM_CLIENTS_DATA)))

    try:
        await wait_for_peers(server, NUM_CLIENTS_DATA, timeout=5.0)

        async def _client_roundtrip(client: aiorak.Client, client_id: int):
            """Send NUM_MESSAGES messages and collect all echoed replies."""
            received: list[bytes] = []
            for i in range(NUM_MESSAGES):
                payload = f"client{client_id}-msg{i}".encode()
                await client.send(payload, reliability=aiorak.Reliability.RELIABLE_ORDERED)

            for _ in range(NUM_MESSAGES):
                data = await asyncio.wait_for(client.recv(), timeout=3.0)
                received.append(data)

            # Verify all messages echoed back correctly and in order.
            for i, data in enumerate(received):
                expected = f"client{client_id}-msg{i}".encode()
                assert data == expected, f"Client {client_id} expected {expected!r} at index {i}, got {data!r}"

        await asyncio.gather(*(_client_roundtrip(c, idx) for idx, c in enumerate(clients)))

        # Verify no connections were lost during the exchange.
        assert len(server._peers) == NUM_CLIENTS_DATA
    finally:
        await asyncio.gather(*(c.close() for c in clients))
