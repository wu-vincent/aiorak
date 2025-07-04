import asyncio
import struct
from datetime import datetime

import aiorak
from aiorak.server import serve


async def handle_client(client_conn: aiorak.ServerConnection):
    pass


async def main():
    async with serve(handle_client, "0.0.0.0", 19132) as server:
        while True:
            server_name = "aiorak"
            level_name = datetime.now().isoformat()
            motd = f"MCPE;{server_name};819;1.21.93;0;50;{server.guid};{level_name};Survival;1;19132;19133;0;"
            server.offline_ping_message = struct.pack(">H", len(motd)) + motd.encode("utf-8")
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
