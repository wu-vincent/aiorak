"""Minimal echo client — sends 5 messages and prints replies."""

import asyncio
import sys

import aiorak

ID_USER = b"\x86"


async def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 19132

    client = await aiorak.connect((host, port), timeout=10.0)
    print(f"Connected to {host}:{port}")

    try:
        for i in range(5):
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
                if reply_count >= 5:
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
