# aiorak

[![PyPI](https://img.shields.io/pypi/v/aiorak)](https://pypi.org/project/aiorak/)
[![Python](https://img.shields.io/pypi/pyversions/aiorak)](https://pypi.org/project/aiorak/)
[![CI](https://github.com/wu-vincent/aiorak/actions/workflows/ci.yml/badge.svg)](https://github.com/wu-vincent/aiorak/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/wu-vincent/aiorak/branch/main/graph/badge.svg)](https://codecov.io/gh/wu-vincent/aiorak)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

[RakNet](http://www.jenkinssoftware.com/) reliable UDP implementation for Python asyncio, written from scratch using the C++ source as reference.

- **Wire-compatible** with RakNet 4.x peers, including Minecraft Bedrock Edition
- **asyncio-native** — `async for`, context managers, no threads
- **All 8 reliability modes** with sliding-window congestion control
- **Fully typed** (`py.typed`) with zero runtime dependencies
- **Python 3.10+**

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

### Ping

```python
import aiorak

response = await aiorak.ping(("127.0.0.1", 19132), timeout=3.0)
print(f"Pong from {response.address}, server GUID: {response.guid}")
```

### Error handling

```python
import aiorak

try:
    client = await aiorak.connect(("127.0.0.1", 19132), timeout=5.0)
except aiorak.RakNetTimeoutError:
    print("Server did not respond")
except aiorak.ConnectionRejectedError as e:
    print(f"Connection rejected: {e}")

# All aiorak exceptions inherit from aiorak.RakNetError
try:
    client.send(b"data")
except aiorak.RakNetError as e:
    print(f"aiorak error: {e}")
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT
