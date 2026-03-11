# Cross-Check Report: Python aiorak vs C++ RakNet

**Last updated:** 2026-03-11

## Summary

All 7 Python modules were audited against the original C++ RakNet source. **All identified behavioral issues have been
fixed** (21 total). The wire format is fully accurate, and the core reliability/connection behavior closely matches C++.
The only remaining items are API-level feature gaps and minor non-impactful differences.

---

## Remaining Differences by Module

### 1. `_bitstream.py`

- `write_uint8`/`read_uint8`/`write_bytes`/`read_bytes` align to byte boundary before read/write; C++ handles unaligned
  via `WriteBits`. Protocol always uses aligned positions — no wire impact.
- Missing features (12 categories) are game-specific helpers (float16, 3D math, delta encoding, string serialization) —
  none affect core protocol.

### 2. `_wire.py`

- `decode_message_frame` includes ACK_RECEIPT types (5-7) in `is_reliable` check — these never appear on wire from valid
  peers. No impact.

### 3. `_connection.py`

| Item                            | Severity | Details                                                                                                        |
|---------------------------------|----------|----------------------------------------------------------------------------------------------------------------|
| Duplicate GUID checking missing | Low      | C++ checks both IP and GUID for duplicate connections at OCR2. Python only checks address at the server layer. |

### 4. `_reliability.py`

Non-impactful missing features:

- Dead connection detection at reliability layer (handled at connection layer instead)
- ACK receipt notifications (`ID_SND_RECEIPT_ACKED`/`ID_SND_RECEIPT_LOSS`)
- Download progress notifications (`ID_DOWNLOAD_PROGRESS`)

### 5. `_server.py` + `_client.py` — API-level gaps only

These are **feature gaps**, not wire-compatibility issues:

| Item                                 | Severity | Details                                                              |
|--------------------------------------|----------|----------------------------------------------------------------------|
| Connection rejection at OCR1 vs OCR2 | Low      | Python rejects at OCR1; C++ at OCR2 (after MTU negotiation).         |
| No server-side broadcast             | Low      | C++ can broadcast to all peers; Python has per-connection send only. |
| Ban list                             | Low      | `AddToBanList()`, `RemoveFromBanList()`, `IsBanned()`                |
| Connection frequency limiting        | Low      | `SetLimitIPConnectionFrequency()`                                    |
| Incoming password                    | Low      | `SetIncomingPassword()` / `GetIncomingPassword()`                    |
| Per-peer disconnect from server      | Low      | `CloseConnection()` with configurable notification                   |
| Configurable timeout                 | Low      | `SetTimeoutTime()` per-connection or global                          |
| Client-side unconnected ping         | Low      | `Ping(host, port)` for server discovery                              |
| Security/encryption                  | Low      | `InitializeSecurity()` with public/private keys                      |
| Plugin system                        | Low      | `AttachPlugin()` / `DetachPlugin()`                                  |
| MTU query API                        | Low      | `GetMTUSize()` — MTU is stored internally but not exposed            |
