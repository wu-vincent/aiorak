"""Offline ping - starts a server, sends unconnected pings, measures RTT.

Uses the ``aiorak.ping()`` API for lightweight latency measurement
without establishing a full connection.  Also demonstrates
``set_offline_ping_response()`` for attaching custom discovery data.
"""

import argparse
import asyncio

import aiorak


async def main():
    parser = argparse.ArgumentParser(description="RakNet offline ping")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="host to ping (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=0, help="server port (default: 0 = ephemeral)")
    parser.add_argument("-n", "--count", type=int, default=10, help="number of pings to send (default: 10)")
    args = parser.parse_args()

    # Start a server with custom offline ping data
    async def _noop(_conn):
        pass

    server = await aiorak.create_server(("0.0.0.0", args.port), _noop, max_connections=10)
    server.set_offline_ping_response(b"My Game Server v1.0")
    actual_port = server.address[1]
    print(f"Ping server on :{actual_port}, sending {args.count} pings...")

    rtts = []
    try:
        for i in range(args.count):
            try:
                resp = await aiorak.ping((args.host, actual_port), timeout=2.0)
                rtts.append(resp.latency_ms)
                print(
                    f"  ping {i + 1}/{args.count}: {resp.latency_ms:.2f} ms"
                    f"  guid={resp.server_guid!r}"
                    f"  data={resp.data!r}"
                )
            except asyncio.TimeoutError:
                print(f"  ping {i + 1}/{args.count}: timeout")
            await asyncio.sleep(0.5)
    finally:
        await server.close()

    if rtts:
        print(f"\n--- {len(rtts)}/{args.count} replies ---")
        print(f"min={min(rtts):.2f} ms  avg={sum(rtts) / len(rtts):.2f} ms  max={max(rtts):.2f} ms")
    else:
        print("\nNo replies received.")


if __name__ == "__main__":
    asyncio.run(main())
