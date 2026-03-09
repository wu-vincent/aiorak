# aiorak

Clean-room [RakNet](http://www.jenkinssoftware.com/) reliable UDP implementation for Python asyncio.

Wire-compatible with RakNet 4.x peers — implements the protocol from scratch without porting the C++ code.

## Features

- Full RakNet connection handshake with MTU discovery
- Reliability modes: unreliable, reliable, ordered, sequenced (all 8 modes)
- Split-packet reassembly for messages exceeding the MTU
- Sliding-window congestion control (mirrors `CCRakNetSlidingWindow`)
- ACK/NAK-based retransmission with RTO estimation
- 32 independent ordering channels
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
    print(f"{conn.address} connected")
    async for data in conn:
        await conn.send(data)  # echo
    print(f"{conn.address} disconnected")

server = await aiorak.create_server(("0.0.0.0", 19132), handler, max_connections=64)
await server.serve_forever()
```

### Client

```python
import aiorak

client = await aiorak.connect(("127.0.0.1", 19132))
await client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
async for data in client:
    print("Got:", data)
```

## API

### Top-level functions

| Function                                                                    | Description                                                  |
|-----------------------------------------------------------------------------|--------------------------------------------------------------|
| `aiorak.create_server(address, handler, max_connections=64, *, guid=None)`  | Bind a UDP socket and start accepting connections.           |
| `aiorak.connect(address, *, timeout=10.0, guid=None)`                       | Connect to a RakNet server with MTU discovery and handshake. |
| `aiorak.ping(address, *, timeout=3.0, only_if_open=False)`                  | Send an unconnected ping without establishing a connection.  |

### `Server`

| Method / Property                          | Description                                    |
|--------------------------------------------|------------------------------------------------|
| `await server.serve_forever()`             | Block until the server is closed.              |
| `await server.close()`                     | Gracefully shut down and disconnect all peers. |
| `server.local_address`                     | The `(host, port)` the server is bound to.     |
| `server.set_offline_ping_response(data)`   | Set custom data for unconnected pong replies.  |
| `async with server:`                       | Context manager — closes on exit.              |

### `Client`

| Method / Property                               | Description                                    |
|-------------------------------------------------|------------------------------------------------|
| `await client.send(data, reliability, channel)` | Send a message to the server.                  |
| `await client.recv()`                           | Wait for and return the next received packet.  |
| `async for data in client`                      | Iterate over received packets as `bytes`.      |
| `await client.close()`                          | Disconnect and release resources.              |
| `client.is_connected`                           | `True` if the handshake is complete.           |
| `client.local_address`                          | The local `(host, port)` of the client socket. |

### `Connection`

| Method / Property                              | Description                                    |
|------------------------------------------------|------------------------------------------------|
| `await conn.send(data, reliability, channel)`  | Send a message to this peer.                   |
| `await conn.recv()`                            | Wait for and return the next received packet.  |
| `async for data in conn`                       | Iterate over received packets as `bytes`.      |
| `await conn.close()`                           | Disconnect this peer.                          |
| `conn.address`                                 | The remote `(host, port)` of the peer.         |

### Enums

- **`Reliability`** — `UNRELIABLE`, `UNRELIABLE_SEQUENCED`, `RELIABLE`, `RELIABLE_ORDERED`, `RELIABLE_SEQUENCED`,
  `UNRELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_ORDERED_WITH_ACK_RECEIPT`
- **`Priority`** — `IMMEDIATE`, `HIGH`, `MEDIUM`, `LOW`

## Requirements

Python 3.10+. No third-party runtime dependencies.

## License

MIT
