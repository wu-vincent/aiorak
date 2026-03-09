"""Minimal echo server — replies to every message with the same data."""

import asyncio
import sys

import aiorak


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19132

    server = await aiorak.create_server(("0.0.0.0", port), max_connections=32)
    print(f"Echo server listening on {server.local_address}")

    try:
        async for event in server:
            if event.type == aiorak.EventType.CONNECT:
                print(f"[+] {event.address[0]}:{event.address[1]} connected")
            elif event.type == aiorak.EventType.DISCONNECT:
                print(f"[-] {event.address[0]}:{event.address[1]} disconnected")
            elif event.type == aiorak.EventType.RECEIVE:
                await server.send(event.address, event.data)
    except KeyboardInterrupt:
        pass
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
