"""Interactive chat client - reads stdin, sends messages, prints received text."""

import argparse
import asyncio
import sys

import aiorak

ID_USER = b"\x86"


async def main():
    parser = argparse.ArgumentParser(description="RakNet interactive chat client")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="server host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=19132, help="server port (default: 19132)")
    args = parser.parse_args()

    client = await aiorak.connect((args.host, args.port), timeout=10.0)
    print(f"Connected to {args.host}:{args.port}. Type messages and press Enter.")

    done = False

    async def receive_loop():
        nonlocal done
        async for data in client:
            if data[:1] == ID_USER:
                print(data[1:].decode(errors="replace"))
        done = True
        print("Disconnected from server.")

    async def input_loop():
        nonlocal done
        while not done:
            line = await asyncio.to_thread(sys.stdin.readline)
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
