"""Minimal echo client — sends 5 messages and prints replies."""

import argparse
import asyncio

import aiorak

ID_USER = b"\x86"


async def main():
    parser = argparse.ArgumentParser(description="RakNet echo client")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="server host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=19132, help="server port (default: 19132)")
    parser.add_argument("-n", "--count", type=int, default=5, help="number of messages to send (default: 5)")
    args = parser.parse_args()

    client = await aiorak.connect((args.host, args.port), timeout=10.0)
    print(f"Connected to {args.host}:{args.port}")

    try:
        for i in range(args.count):
            msg = ID_USER + f"Hello #{i}".encode()
            await client.send(msg)
            print(f"Sent: Hello #{i}")
            await asyncio.sleep(1.0)

        # Collect replies
        reply_count = 0
        async for event in client:
            if event.type == aiorak.EventType.RECEIVE:
                text = event.data[1:].decode() if event.data[:1] == ID_USER else repr(event.data)
                print(f"Reply: {text}")
                reply_count += 1
                if reply_count >= args.count:
                    break
            elif event.type == aiorak.EventType.DISCONNECT:
                print("Disconnected from server")
                break
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
