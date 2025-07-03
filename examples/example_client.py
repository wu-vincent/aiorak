import asyncio

from aiorak.client import connect


async def main():
    client = await connect("test.endstone.dev", 19132)
    print("Connected")
    while True:
        data, reliability = await client.receive()
        print(reliability.name, data.hex(sep=" "))


if __name__ == "__main__":
    asyncio.run(main())
