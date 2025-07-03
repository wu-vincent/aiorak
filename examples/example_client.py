import asyncio

from aiorak.client import connect
from aiorak.stream import ByteStream


async def main():
    client = await connect("test.endstone.dev", 19132)
    print("Connected")
    stream = ByteStream()
    stream.write_byte(193)
    stream.write_int(818)
    client.send(stream.data, reliable=True)
    while True:
        data, reliability = await client.receive()
        print(reliability.name, data.hex(sep=" "))


if __name__ == "__main__":
    asyncio.run(main())
