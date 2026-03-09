"""Multi-client stress test — spawns N clients that each send M messages."""

import argparse
import asyncio
import time

import aiorak

ID_USER = b"\x86"


async def client_task(client_id, host, port, num_messages):
    """Connect, send messages, collect replies, disconnect."""
    client = await aiorak.connect((host, port), timeout=10.0)
    sent = 0
    received = 0

    async def receive_loop():
        nonlocal received
        async for data in client:
            received += 1
            if received >= num_messages:
                break

    recv_task = asyncio.create_task(receive_loop())

    try:
        for i in range(num_messages):
            msg = ID_USER + f"client{client_id}-msg{i}".encode()
            await client.send(msg)
            sent += 1
            await asyncio.sleep(0.1)

        # Wait for remaining replies (with timeout)
        try:
            await asyncio.wait_for(recv_task, timeout=5.0)
        except asyncio.TimeoutError:
            recv_task.cancel()
    finally:
        await client.close()

    return sent, received


async def handler(conn: aiorak.Connection):
    """Echo handler for the server."""
    async for data in conn:
        await conn.send(data)


async def main():
    parser = argparse.ArgumentParser(description="RakNet multi-client stress test")
    parser.add_argument("-c", "--clients", type=int, default=5, help="number of clients (default: 5)")
    parser.add_argument("-n", "--messages", type=int, default=10, help="messages per client (default: 10)")
    args = parser.parse_args()

    # Start echo server on ephemeral port
    server = await aiorak.create_server(("0.0.0.0", 0), handler, max_connections=args.clients + 1)
    host, port = "127.0.0.1", server.local_address[1]
    print(f"Server on :{port}, launching {args.clients} clients x {args.messages} messages")

    t0 = time.monotonic()
    results = await asyncio.gather(
        *(client_task(i, host, port, args.messages) for i in range(args.clients))
    )
    elapsed = time.monotonic() - t0

    await server.close()

    total_sent = sum(s for s, _ in results)
    total_recv = sum(r for _, r in results)
    print(f"\n--- Summary ---")
    print(f"Clients: {args.clients}")
    print(f"Sent:    {total_sent}")
    print(f"Received:{total_recv}")
    print(f"Time:    {elapsed:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
