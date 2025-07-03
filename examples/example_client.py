import asyncio

from aiorak.client import connect


async def main():
    client = await connect("test.endstone.dev", 19132)
    print("Connected")
    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
