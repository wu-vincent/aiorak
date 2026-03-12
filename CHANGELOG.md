# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-03-12

Initial release.

### Added

- RakNet 4.x wire-compatible protocol implementation in pure Python (zero runtime dependencies)
- asyncio-native server with per-peer handler coroutines and `async for` iteration
- asyncio-native client with `send()`/`recv()`/`async for`
- All 8 reliability modes, 32 ordering channels, split-packet reassembly
- TCP-like sliding-window congestion control (based on `CCRakNetSlidingWindow.cpp`)
- Full 6-message handshake: 3-message offline phase (MTU discovery) + 3-message reliable phase
- Offline ping/pong via `aiorak.ping()` for LAN server discovery
- Server broadcast, per-peer disconnect, configurable timeouts, MTU query
- Connection rate limiting and max connection enforcement with GUID duplicate detection
- Configurable protocol version, MTU bounds, discovery sizes, and internal ID count
- IPv4 and IPv6 address support
- `py.typed` marker and type annotations
- CI/CD with GitHub Actions (Ubuntu + Windows, Python 3.10-3.13)

[0.1.0]: https://github.com/wu-vincent/aiorak/releases/tag/v0.1.0
