import asyncio

from aiorak.client import connect
from aiorak.exceptions import DisconnectionError


async def main():
    client = await connect("test.endstone.dev", 19132)
    print("Connected")
    client.send(b"\xfe\x06\xc1\x01\x00\x00\x03\x33", reliable=True)
    while True:
        try:
            data, reliability = await client.receive()
            print(reliability.name, data.hex(sep=" "))
        except DisconnectionError:
            print("Disconnected")
            break


if __name__ == "__main__":
    asyncio.run(main())
