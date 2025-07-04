import asyncio

from aiorak.client import connect, ping
from aiorak.exceptions import DisconnectionError
from aiorak.stream import ByteStream


async def main():
    pong = await ping("test.endstone.dev", 19132)
    stream = ByteStream(pong)
    length = stream.read_short()
    content = stream.read(length)
    print(content.tobytes().decode("utf-8"))

    async with await connect("test.endstone.dev", 19132) as client:
        print("Connected")
        client.send(b"\xfe\x06\xc1\x01\x00\x00\x03\x32", reliable=True)
        while True:
            try:
                data, reliability = await client.receive()
                print(reliability.name, data.hex(sep=" "))
            except DisconnectionError:
                print("Disconnected")
                break


if __name__ == "__main__":
    asyncio.run(main())
