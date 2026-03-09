"""Interactive chat client — reads stdin, sends messages, prints received text.

Adapted from the C++ ChatExampleClient sample.
Uses run_in_executor for stdin so it works on Windows.
"""

import argparse
import asyncio
import sys

import aiorak

ID_USER = b"\x86"


async def read_input(loop):
    """Read a line from stdin without blocking the event loop."""
    return await loop.run_in_executor(None, sys.stdin.readline)


async def main():
    parser = argparse.ArgumentParser(description="RakNet interactive chat client")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="server host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=19132, help="server port (default: 19132)")
    args = parser.parse_args()

    client = await aiorak.connect((args.host, args.port), timeout=10.0)
    print(f"Connected to {args.host}:{args.port}. Type messages and press Enter.")

    loop = asyncio.get_running_loop()
    done = False

    async def receive_loop():
        nonlocal done
        async for event in client:
            if event.type == aiorak.EventType.RECEIVE:
                if event.data[:1] == ID_USER:
                    print(event.data[1:].decode(errors="replace"))
            elif event.type == aiorak.EventType.DISCONNECT:
                print("Disconnected from server.")
                done = True
                break

    async def input_loop():
        nonlocal done
        while not done:
            line = await read_input(loop)
            if not line:
                break
            text = line.rstrip("\n")
            if not text:
                continue
            await client.send(ID_USER + text.encode())

    try:
        await asyncio.gather(receive_loop(), input_loop())
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
