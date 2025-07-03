import asyncio

from aiorak.client import connect
from aiorak.stream import ByteStream


async def main():
    client = await connect("test.endstone.dev", 19132)
    print("Connected")
    client.send(b"\xfe\x06\xc1\x01\x00\x00\x03\x32", reliable=True)
    while True:
        data, reliability = await client.receive()
        print(reliability.name, data.hex(sep=" "))


if __name__ == "__main__":
    asyncio.run(main())
