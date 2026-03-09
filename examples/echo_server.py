"""Minimal echo server — replies to every message with the same data."""

import argparse
import asyncio

import aiorak


async def main():
    parser = argparse.ArgumentParser(description="RakNet echo server")
    parser.add_argument("-p", "--port", type=int, default=19132, help="port to listen on (default: 19132)")
    parser.add_argument("--max-connections", type=int, default=32, help="max simultaneous connections (default: 32)")
    args = parser.parse_args()

    server = await aiorak.create_server(("0.0.0.0", args.port), max_connections=args.max_connections)
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
