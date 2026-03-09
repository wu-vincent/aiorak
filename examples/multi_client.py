"""Multi-client stress test — spawns N clients that each send M messages.

Demonstrates concurrent async I/O with asyncio.gather().
"""

import asyncio
import sys
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
        async for event in client:
            if event.type == aiorak.EventType.RECEIVE:
                received += 1
                if received >= num_messages:
                    break
            elif event.type == aiorak.EventType.DISCONNECT:
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


async def main():
    num_clients = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    num_messages = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    # Start echo server on ephemeral port
    server = await aiorak.create_server(("0.0.0.0", 0), max_connections=num_clients + 1)
    host, port = "127.0.0.1", server.local_address[1]
    print(f"Server on :{port}, launching {num_clients} clients x {num_messages} messages")

    async def server_loop():
        async for event in server:
            if event.type == aiorak.EventType.RECEIVE:
                await server.send(event.address, event.data)

    server_task = asyncio.create_task(server_loop())

    t0 = time.monotonic()
    results = await asyncio.gather(
        *(client_task(i, host, port, num_messages) for i in range(num_clients))
    )
    elapsed = time.monotonic() - t0

    server_task.cancel()
    await server.close()

    total_sent = sum(s for s, _ in results)
    total_recv = sum(r for _, r in results)
    print(f"\n--- Summary ---")
    print(f"Clients: {num_clients}")
    print(f"Sent:    {total_sent}")
    print(f"Received:{total_recv}")
    print(f"Time:    {elapsed:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
