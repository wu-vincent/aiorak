# aiorak

[![PyPI](https://img.shields.io/pypi/v/aiorak)](https://pypi.org/project/aiorak/)
[![Python](https://img.shields.io/pypi/pyversions/aiorak)](https://pypi.org/project/aiorak/)
[![CI](https://github.com/wu-vincent/aiorak/actions/workflows/ci.yml/badge.svg)](https://github.com/wu-vincent/aiorak/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/wu-vincent/aiorak/branch/main/graph/badge.svg)](https://codecov.io/gh/wu-vincent/aiorak)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

[RakNet](http://www.jenkinssoftware.com/) reliable UDP implementation for Python asyncio, written from scratch using the C++ source as reference.

Wire-compatible with RakNet 4.x peers. Zero runtime dependencies.

## Why aiorak?

| Feature | aiorak | rakpy | pyraklib |
|---|---|---|---|
| asyncio-native | Yes | No | No |
| All 8 reliability modes | Yes | Partial | Partial |
| Congestion control | Yes (sliding window) | No | No |
| Fully typed (`py.typed`) | Yes | No | No |
| Zero runtime dependencies | Yes | No | No |
| Python 3.10+ | Yes | — | — |

aiorak is designed for applications that communicate with RakNet 4.x peers — including Minecraft Bedrock Edition and other games using the RakNet protocol.

## Features

- Full RakNet connection handshake with MTU discovery
- Reliability modes: unreliable, reliable, ordered, sequenced (all 8 modes)
- Split-packet reassembly for messages exceeding the MTU
- Sliding-window congestion control (mirrors `CCRakNetSlidingWindow`)
- ACK/NAK-based retransmission with RTO estimation
- 32 independent ordering channels
- Send-queue priority levels (immediate, high, medium, low)
- Fully typed with `py.typed` marker

## Installation

```bash
pip install aiorak
```

## Quick start

### Server

```python
import aiorak

async def handler(conn: aiorak.Connection):
    print(f"{conn.remote_address} connected")
    async for data in conn:
        conn.send(data)  # echo
    print(f"{conn.remote_address} disconnected")

server = await aiorak.create_server(("0.0.0.0", 19132), handler, max_connections=64)
await server.serve_forever()
```

### Client

```python
import aiorak

client = await aiorak.connect(("127.0.0.1", 19132))
client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
async for data in client:
    print("Got:", data)
```

### Error handling

```python
import asyncio
import aiorak

try:
    client = await aiorak.connect(("127.0.0.1", 19132), timeout=5.0)
except asyncio.TimeoutError:
    print("Server did not respond")
except ConnectionRefusedError as e:
    print(f"Connection rejected: {e}")

# Catch all aiorak errors
try:
    client.send(b"data")
except aiorak.RakNetError as e:
    print(f"aiorak error: {e}")
```

## API

### Top-level functions

| Function | Description |
|---|---|
| `await create_server(address, handler, max_connections=64, *, guid, protocol_version, max_mtu, min_mtu, num_internal_ids, timeout, rate_limit_ips)` | Bind a UDP socket and start accepting connections. |
| `await connect(address, *, timeout=10.0, guid, protocol_version, max_mtu, min_mtu, mtu_discovery_sizes, num_internal_ids)` | Connect to a RakNet server with MTU discovery and handshake. |
| `await ping(address, *, timeout=3.0, only_if_open=False)` | Send an unconnected ping without establishing a connection. |

### `Server`

| Method / Property | Description |
|---|---|
| `await server.serve_forever()` | Block until the server is closed. |
| `await server.close()` | Gracefully shut down and disconnect all peers. |
| `server.broadcast(data, reliability, channel, priority, *, exclude)` | Send a message to all connected peers. |
| `server.disconnect(conn, *, notify=True)` | Disconnect a specific peer. |
| `server.set_offline_ping_response(data)` | Set custom data for unconnected pong replies. |
| `server.set_timeout(timeout, conn=None)` | Set timeout for a connection or all connections. |
| `server.get_timeout(conn=None)` | Get timeout for a connection or the server default. |
| `server.get_mtu(conn)` | Get the negotiated MTU for a connection. |
| `server.address` | The `(host, port)` the server is bound to. |
| `server.guid` | The 64-bit server GUID. |
| `server.timeout` | Default dead-connection timeout in seconds. |
| `server.connections` | Read-only list of active connections. |
| `server.connection_count` | Number of currently connected peers. |
| `async with server:` | Context manager — closes on exit. |

### `Client`

| Method / Property | Description |
|---|---|
| `client.send(data, reliability, channel, priority)` | Send a message to the server. |
| `async for data in client` | Iterate over received packets as `bytes`. |
| `await client.close()` | Disconnect and release resources. |
| `client.is_connected` | `True` if the handshake is complete. |
| `client.address` | The local `(host, port)` of the client socket. |
| `client.remote_address` | The `(host, port)` of the remote server. |
| `client.guid` | The 64-bit client GUID. |
| `client.mtu` | The negotiated MTU for this connection. |
| `client.timeout` | Dead-connection timeout in seconds. |
| `async with client:` | Context manager — closes on exit. |

### `Connection`

| Method / Property | Description |
|---|---|
| `conn.send(data, reliability, channel, priority)` | Send a message to this peer. |
| `async for data in conn` | Iterate over received packets as `bytes`. |
| `await conn.close()` | Disconnect this peer. |
| `conn.remote_address` | The remote `(host, port)` of the peer. |
| `conn.mtu` | The negotiated MTU for this connection. |
| `conn.guid` | Our 64-bit GUID. |
| `conn.remote_guid` | The remote peer's 64-bit GUID. |

### Enums

- **`Reliability`** — `UNRELIABLE`, `UNRELIABLE_SEQUENCED`, `RELIABLE`, `RELIABLE_ORDERED`, `RELIABLE_SEQUENCED`,
  `UNRELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_ORDERED_WITH_ACK_RECEIPT`
- **`Priority`** — `IMMEDIATE`, `HIGH`, `MEDIUM`, `LOW`

### Exceptions

All exceptions inherit from `RakNetError`:

- **`RakNetError`** — Base exception for all aiorak errors
- **`HandshakeError`** — Connection handshake failures
- **`ConnectionClosedError`** — Operations on a closed connection
- **`ProtocolError`** — Wire protocol violations

### Logging

aiorak uses the standard `logging` module. To enable debug output:

```python
import logging
logging.getLogger("aiorak").setLevel(logging.DEBUG)
```

## Requirements

Python 3.10+. No third-party runtime dependencies.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT
