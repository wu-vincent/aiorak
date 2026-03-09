"""Port of the C++ EightPeerTest to Python pytest.

Eight clients connect to a server in a client-server topology.  The server
broadcasts every incoming message to all *other* connected clients, emulating
a full-mesh overlay.  Each client sends 100 RELIABLE_ORDERED packets tagged
with its sender ID and a sequence number.  On the receiving side we verify
that, for every (receiver, sender) pair, messages arrived in strictly
ascending order.

Expected totals:
  - Each client sends  : 100 messages
  - Server fans out to : 7 other clients  =>  700 receives per client
  - Grand total        : 8 * 700 = 5,600 received messages
"""

from __future__ import annotations

import asyncio
import struct
from collections import defaultdict

import pytest

import aiorak


async def wait_for_peers(server, count, timeout=5.0):
    """Wait until server has at least count connected peers."""
    async def _wait():
        while len(server._peers) < count:
            await asyncio.sleep(0.02)
    await asyncio.wait_for(_wait(), timeout=timeout)


pytestmark = pytest.mark.asyncio

NUM_CLIENTS = 8
MESSAGES_PER_CLIENT = 100
EXPECTED_PER_CLIENT = MESSAGES_PER_CLIENT * (NUM_CLIENTS - 1)  # 700
EXPECTED_TOTAL = NUM_CLIENTS * EXPECTED_PER_CLIENT  # 5600
PADDING = b"\xAB" * 200  # bulk up the packet a bit


@pytest.mark.slow
async def test_eight_peer_broadcast(server_factory, client_factory):
    """Eight clients exchange 100 reliable-ordered messages each via broadcast."""

    # -- broadcast handler ------------------------------------------------
    clients: dict[tuple[str, int], aiorak.Connection] = {}

    async def broadcast_handler(conn: aiorak.Connection):
        clients[conn.address] = conn
        try:
            async for data in conn:
                for addr, other in list(clients.items()):
                    if addr != conn.address:
                        await other.send(data)
        finally:
            clients.pop(conn.address, None)

    # -- set up server and clients ----------------------------------------
    server = await server_factory(
        handler=broadcast_handler,
        max_connections=NUM_CLIENTS + 2,
    )
    addr = server.local_address

    peers: list[aiorak.Client] = []
    for _ in range(NUM_CLIENTS):
        cli = await client_factory(addr, timeout=10.0)
        peers.append(cli)

    await wait_for_peers(server, NUM_CLIENTS, timeout=15.0)

    # -- receiver logic ---------------------------------------------------
    # received[receiver_id] = {sender_id: [seq0, seq1, ...]}
    received: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    recv_counts: list[int] = [0] * NUM_CLIENTS

    async def receiver(client_idx: int, client: aiorak.Client):
        while recv_counts[client_idx] < EXPECTED_PER_CLIENT:
            try:
                data = await asyncio.wait_for(client.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                break
            sender_id = data[0]
            (seq,) = struct.unpack_from(">I", data, 1)
            received[client_idx][sender_id].append(seq)
            recv_counts[client_idx] += 1

    # -- sender logic -----------------------------------------------------
    async def sender(client_idx: int, client: aiorak.Client):
        for seq in range(MESSAGES_PER_CLIENT):
            payload = (
                bytes([client_idx])
                + struct.pack(">I", seq)
                + PADDING
            )
            await client.send(
                payload,
                reliability=aiorak.Reliability.RELIABLE_ORDERED,
                channel=0,
            )
            # Pace sends to avoid overwhelming the transport
            await asyncio.sleep(0.01)

    # -- launch all senders and receivers concurrently --------------------
    tasks: list[asyncio.Task] = []
    for idx, cli in enumerate(peers):
        tasks.append(asyncio.create_task(receiver(idx, cli)))
        tasks.append(asyncio.create_task(sender(idx, cli)))

    # Wait for all tasks with a generous overall timeout
    done, pending = await asyncio.wait(tasks, timeout=60.0)
    for t in pending:
        t.cancel()
    # Let cancellations propagate
    await asyncio.gather(*pending, return_exceptions=True)

    # Re-raise any unexpected exceptions from completed tasks
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            raise exc

    # -- verification -----------------------------------------------------
    total_received = sum(recv_counts)
    assert total_received == EXPECTED_TOTAL, (
        f"Expected {EXPECTED_TOTAL} total messages, got {total_received}"
    )

    # Per-sender ordering: within each (receiver, sender) pair the sequence
    # numbers must be strictly monotonically increasing.
    for receiver_idx in range(NUM_CLIENTS):
        for sender_idx in range(NUM_CLIENTS):
            if sender_idx == receiver_idx:
                # A client never receives its own messages
                assert sender_idx not in received[receiver_idx]
                continue
            seqs = received[receiver_idx][sender_idx]
            assert len(seqs) == MESSAGES_PER_CLIENT, (
                f"Receiver {receiver_idx} got {len(seqs)} messages from "
                f"sender {sender_idx}, expected {MESSAGES_PER_CLIENT}"
            )
            for i in range(1, len(seqs)):
                assert seqs[i] > seqs[i - 1], (
                    f"Out-of-order delivery: receiver={receiver_idx} "
                    f"sender={sender_idx} seq[{i - 1}]={seqs[i - 1]} "
                    f"seq[{i}]={seqs[i]}"
                )
