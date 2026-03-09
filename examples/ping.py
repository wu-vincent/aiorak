"""Ping measurement — starts a server and client, measures round-trip times.

Since aiorak doesn't expose the offline ping/pong API, this example
uses connected ping: the client sends timestamped messages and the
server echoes them back, allowing RTT calculation.
"""

import asyncio
import struct
import sys
import time

import aiorak

ID_USER = b"\x86"


async def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    # Start echo server on an ephemeral port
    server = await aiorak.create_server(("0.0.0.0", port), max_connections=1)
    actual_port = server.local_address[1]
    print(f"Ping server on :{actual_port}, sending {count} pings...")

    async def server_loop():
        async for event in server:
            if event.type == aiorak.EventType.RECEIVE:
                await server.send(event.address, event.data)

    server_task = asyncio.create_task(server_loop())

    client = await aiorak.connect((host, actual_port), timeout=5.0)

    rtts = []
    received = asyncio.Event()
    last_rtt = 0.0

    async def receive_loop():
        nonlocal last_rtt
        async for event in client:
            if event.type == aiorak.EventType.RECEIVE:
                if len(event.data) >= 9 and event.data[:1] == ID_USER:
                    send_time = struct.unpack("!d", event.data[1:9])[0]
                    last_rtt = (time.monotonic() - send_time) * 1000
                    rtts.append(last_rtt)
                    received.set()

    recv_task = asyncio.create_task(receive_loop())

    try:
        for i in range(count):
            payload = ID_USER + struct.pack("!d", time.monotonic())
            await client.send(payload)
            received.clear()
            try:
                await asyncio.wait_for(received.wait(), timeout=2.0)
                print(f"  ping {i + 1}/{count}: {last_rtt:.2f} ms")
            except asyncio.TimeoutError:
                print(f"  ping {i + 1}/{count}: timeout")
            await asyncio.sleep(0.5)
    finally:
        recv_task.cancel()
        server_task.cancel()
        await client.close()
        await server.close()

    if rtts:
        print(f"\n--- {len(rtts)}/{count} replies ---")
        print(f"min={min(rtts):.2f} ms  avg={sum(rtts)/len(rtts):.2f} ms  max={max(rtts):.2f} ms")
    else:
        print("\nNo replies received.")


if __name__ == "__main__":
    asyncio.run(main())
