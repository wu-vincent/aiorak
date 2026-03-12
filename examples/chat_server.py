"""Chat relay server - broadcasts messages from any client to all others."""

import argparse
import asyncio

import aiorak

ID_USER = b"\x86"

# Shared state
connections: dict[tuple[str, int], aiorak.Connection] = {}


async def broadcast(data: bytes, *, exclude: tuple[str, int] | None = None):
    """Send data to all connected peers except the excluded one."""
    for addr, conn in list(connections.items()):
        if addr != exclude:
            try:
                await conn.send(data)
            except RuntimeError:
                pass


async def handler(conn: aiorak.Connection):
    tag = f"{conn.address[0]}:{conn.address[1]}"
    connections[conn.address] = conn
    print(f"[+] {tag} joined ({len(connections)} online)")
    notice = ID_USER + f"*** {tag} joined the chat ***".encode()
    await broadcast(notice, exclude=conn.address)

    try:
        async for data in conn:
            if data[:1] != ID_USER:
                continue
            text = data[1:].decode(errors="replace")
            print(f"<{tag}> {text}")
            relay = ID_USER + f"<{tag}> {text}".encode()
            await broadcast(relay, exclude=conn.address)
    finally:
        connections.pop(conn.address, None)
        print(f"[-] {tag} left ({len(connections)} online)")
        notice = ID_USER + f"*** {tag} left the chat ***".encode()
        await broadcast(notice)


async def main():
    parser = argparse.ArgumentParser(description="RakNet chat relay server")
    parser.add_argument("-p", "--port", type=int, default=19132, help="port to listen on (default: 19132)")
    parser.add_argument("--max-connections", type=int, default=32, help="max simultaneous connections (default: 32)")
    args = parser.parse_args()

    server = await aiorak.create_server(("0.0.0.0", args.port), handler, max_connections=args.max_connections)
    print(f"Chat server listening on {server.local_address}")

    try:
        await server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
