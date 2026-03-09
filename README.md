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

server = await aiorak.create_server(("0.0.0.0", 19132), max_connections=64)
async for event in server:
    if event.type == aiorak.EventType.CONNECT:
        print(f"Peer connected: {event.address}")
    elif event.type == aiorak.EventType.RECEIVE:
        await server.send(event.address, b"reply",
                          reliability=aiorak.Reliability.RELIABLE_ORDERED)
```

### Client

```python
import aiorak

client = await aiorak.connect(("127.0.0.1", 19132))
await client.send(b"hello", reliability=aiorak.Reliability.RELIABLE_ORDERED)
async for event in client:
    if event.type == aiorak.EventType.RECEIVE:
        print("Got:", event.data)
```

## API

### Top-level functions

| Function | Description |
|---|---|
| `aiorak.create_server(address, max_connections=64, *, guid=None)` | Bind a UDP socket and start accepting connections. |
| `aiorak.connect(address, *, timeout=10.0, guid=None)` | Connect to a RakNet server with MTU discovery and handshake. |

### `Server`

| Method / Property | Description |
|---|---|
| `await server.send(address, data, reliability, channel)` | Send a message to a connected peer. |
| `async for event in server` | Iterate over incoming events. |
| `await server.close()` | Gracefully shut down and disconnect all peers. |
| `server.local_address` | The `(host, port)` the server is bound to. |

### `Client`

| Method / Property | Description |
|---|---|
| `await client.send(data, reliability, channel)` | Send a message to the server. |
| `async for event in client` | Iterate over incoming events. |
| `await client.close()` | Disconnect and release resources. |
| `client.is_connected` | `True` if the handshake is complete. |
| `client.local_address` | The local `(host, port)` of the client socket. |

### Enums

- **`Reliability`** — `UNRELIABLE`, `UNRELIABLE_SEQUENCED`, `RELIABLE`, `RELIABLE_ORDERED`, `RELIABLE_SEQUENCED`, `UNRELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_WITH_ACK_RECEIPT`, `RELIABLE_ORDERED_WITH_ACK_RECEIPT`
- **`Priority`** — `IMMEDIATE`, `HIGH`, `MEDIUM`, `LOW`
- **`EventType`** — `CONNECT`, `DISCONNECT`, `RECEIVE`

### `Event`

| Field | Type | Description |
|---|---|---|
| `type` | `EventType` | The kind of event. |
| `address` | `tuple[str, int]` | Remote peer `(host, port)`. |
| `data` | `bytes` | Payload bytes (`RECEIVE` events) or `b""`. |
| `channel` | `int` | Ordering channel (0–31). |

## Requirements

Python 3.10+. No third-party runtime dependencies.

## License

MIT
