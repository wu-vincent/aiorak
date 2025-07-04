#!/usr/bin/env python3
import argparse
import asyncio
import logging

import aiorak
import aiorak.connection

# async def proxy_data(src: aiorak.connection.Connection, dst:  aiorak.connection.Connection):
#     """Relay packets from src → dst until one side closes."""
#     try:
#         async for packet in src:
#             await dst.send(packet)
#     except aiorak.DisconnectionError:
#         pass
#
#
# async def handle_client(client_conn: aiorak.ServerConnection, remote_host: str, remote_port: int):
#     peer = client_conn.external_addr
#     logging.info(f"[client→proxy] {peer} connected")
#     logging.info(f"[proxy→remote] dialing {remote_host}:{remote_port}")
#     try:
#         # connect over UDP to the remote server
#         async with aiorak.connect(remote_host, remote_port) as remote_conn:
#             await asyncio.gather(
#                 proxy_data(client_conn, remote_conn),
#                 proxy_data(remote_conn, client_conn),
#             )
#     except Exception as e:
#         logging.error(f"Proxy error: {e!r}")
#     finally:
#         await client_conn.close()
#         logging.info(f"[proxy] closed connection with {peer}")
#


async def handler(conn: aiorak.ServerConnection):
    pass


async def main():
    p = argparse.ArgumentParser(description="Asyncio RakNet UDP reverse proxy")
    p.add_argument("--listen-host", default="0.0.0.0", help="Interface for incoming RakNet clients")
    p.add_argument("--listen-port", type=int, default=19132, help="Port for incoming RakNet clients")
    p.add_argument("--remote-host", required=True, help="Backend RakNet host (UDP)")
    p.add_argument("--remote-port", type=int, default=19132, help="Backend RakNet port (UDP)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async with aiorak.serve(handler, "0.0.0.0", 19132) as server:
        logging.info(
            f"RakNet proxy listening on {args.listen_host}:{args.listen_port} "
            f"-> forwarding to {args.remote_host}:{args.remote_port}"
        )
        while True:
            try:
                response, _ = await asyncio.gather(
                    aiorak.ping(args.remote_host, args.remote_port, timeout=1.0), asyncio.sleep(1.0)
                )
                server.ping_response = response
                logging.info(response.hex(sep=" "))
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
