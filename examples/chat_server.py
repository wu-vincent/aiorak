"""Chat relay server — broadcasts messages from any client to all others.

Adapted from the C++ ChatExampleServer sample.
"""

import asyncio
import sys

import aiorak

ID_USER = b"\x86"


async def broadcast(server, peers, data, *, exclude=None):
    """Send data to all connected peers except the excluded one."""
    for addr in peers:
        if addr != exclude:
            await server.send(addr, data)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19132

    server = await aiorak.create_server(("0.0.0.0", port), max_connections=32)
    peers: set[tuple[str, int]] = set()
    print(f"Chat server listening on {server.local_address}")

    try:
        async for event in server:
            if event.type == aiorak.EventType.CONNECT:
                peers.add(event.address)
                tag = f"{event.address[0]}:{event.address[1]}"
                print(f"[+] {tag} joined ({len(peers)} online)")
                notice = ID_USER + f"*** {tag} joined the chat ***".encode()
                await broadcast(server, peers, notice, exclude=event.address)

            elif event.type == aiorak.EventType.DISCONNECT:
                peers.discard(event.address)
                tag = f"{event.address[0]}:{event.address[1]}"
                print(f"[-] {tag} left ({len(peers)} online)")
                notice = ID_USER + f"*** {tag} left the chat ***".encode()
                await broadcast(server, peers, notice)

            elif event.type == aiorak.EventType.RECEIVE:
                if event.data[:1] != ID_USER:
                    continue
                text = event.data[1:].decode(errors="replace")
                tag = f"{event.address[0]}:{event.address[1]}"
                print(f"<{tag}> {text}")
                relay = ID_USER + f"<{tag}> {text}".encode()
                await broadcast(server, peers, relay, exclude=event.address)
    except KeyboardInterrupt:
        pass
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
