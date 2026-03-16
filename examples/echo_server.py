"""Minimal echo server - replies to every message with the same data."""

import argparse
import asyncio

import aiorak


async def handler(conn: aiorak.Connection):
    print(f"[+] {conn.remote_address[0]}:{conn.remote_address[1]} connected")
    async for data in conn:
        await conn.send(data)
    print(f"[-] {conn.remote_address[0]}:{conn.remote_address[1]} disconnected")


async def main():
    parser = argparse.ArgumentParser(description="RakNet echo server")
    parser.add_argument("-p", "--port", type=int, default=19132, help="port to listen on (default: 19132)")
    parser.add_argument("--max-connections", type=int, default=32, help="max simultaneous connections (default: 32)")
    args = parser.parse_args()

    server = await aiorak.create_server(("0.0.0.0", args.port), handler, max_connections=args.max_connections)
    print(f"Echo server listening on {server.address}")

    try:
        await server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
