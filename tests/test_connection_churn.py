"""Connection churn tests — rapid connect/disconnect cycles and stress patterns."""

import asyncio
import random

import pytest

import aiorak
from aiorak import Connection, Reliability

from .conftest import wait_for_peers, force_close_transport


class TestChurnCycles:
    async def test_connect_disconnect_10_cycles(self, server_factory):
        """10 sequential connect/exchange/disconnect cycles."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler)
        addr = server.local_address

        for cycle in range(10):
            client = await aiorak.connect(addr, timeout=5.0)
            assert client.is_connected

            await wait_for_peers(server, 1, timeout=3.0)

            await client.send(b"\x86cycle" + bytes([cycle]))
            data = await asyncio.wait_for(received.get(), timeout=5.0)
            assert len(data) > 0

            await client.close()
            # Wait for handler to finish
            await asyncio.sleep(0.3)

    async def test_32_clients_disconnect_reconnect(self, server_factory, client_factory):
        """Connect 32, disconnect all, reconnect 32 new ones."""
        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=64)
        addr = server.local_address

        # First batch: 32 clients
        batch1 = []
        for _ in range(32):
            cli = await client_factory(addr)
            batch1.append(cli)

        await wait_for_peers(server, 32, timeout=15.0)

        # Disconnect all
        for cli in batch1:
            await cli.close()

        # Wait for server to detect disconnects
        async def _wait_no_peers():
            while len(server._peers) > 0:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_no_peers(), timeout=15.0)

        # Second batch: 32 new clients
        batch2 = []
        for _ in range(32):
            cli = await aiorak.connect(addr, timeout=5.0)
            batch2.append(cli)

        await wait_for_peers(server, 32, timeout=15.0)

        for cli in batch2:
            await cli.close()


class TestInterleavedChurn:
    async def test_interleaved_connect_disconnect(self, server_factory, client_factory):
        """Connect 5, disconnect 0/2/4, connect 3 new, verify data flows."""
        received = asyncio.Queue()

        async def handler(conn: Connection):
            async for data in conn:
                received.put_nowait(data)

        server = await server_factory(handler=handler, max_connections=10)
        addr = server.local_address

        clients = []
        for _ in range(5):
            cli = await client_factory(addr)
            clients.append(cli)

        await wait_for_peers(server, 5, timeout=10.0)

        # Disconnect clients 0, 2, 4
        for i in [0, 2, 4]:
            await clients[i].close()

        await asyncio.sleep(0.5)

        # Connect 3 new clients
        new_clients = []
        for _ in range(3):
            cli = await client_factory(addr)
            new_clients.append(cli)

        await asyncio.sleep(0.5)

        # Remaining original clients (1, 3) should still work
        for i in [1, 3]:
            await clients[i].send(b"\x86still_alive")

        for _ in range(2):
            await asyncio.wait_for(received.get(), timeout=5.0)

        # New clients should also work
        for cli in new_clients:
            await cli.send(b"\x86new_client")

        for _ in range(3):
            await asyncio.wait_for(received.get(), timeout=5.0)


class TestAbruptDisconnect:
    async def test_client_destroyed_without_close(
        self, server_factory, client_factory, monkeypatch
    ):
        """Cancel client tasks, server handler ends due to timeout."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 2.0)

        handler_done = asyncio.Event()

        async def handler(conn: Connection):
            async for data in conn:
                pass
            handler_done.set()

        server = await server_factory(handler=handler)
        client = await client_factory(server.local_address)
        await wait_for_peers(server, 1, timeout=3.0)

        force_close_transport(client)

        await asyncio.wait_for(handler_done.wait(), timeout=5.0)

    async def test_multiple_abrupt_disconnects(
        self, server_factory, client_factory, monkeypatch
    ):
        """5 clients force-closed, all handlers end."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 2.0)

        handler_done_count = 0
        all_done = asyncio.Event()

        async def handler(conn: Connection):
            nonlocal handler_done_count
            async for data in conn:
                pass
            handler_done_count += 1
            if handler_done_count >= 5:
                all_done.set()

        server = await server_factory(handler=handler, max_connections=10)

        clients = []
        for _ in range(5):
            cli = await client_factory(server.local_address)
            clients.append(cli)

        await wait_for_peers(server, 5, timeout=10.0)

        for cli in clients:
            force_close_transport(cli)

        await asyncio.wait_for(all_done.wait(), timeout=5.0)


class TestStress:
    async def test_random_operations(self, server_factory, monkeypatch):
        """5 client slots, 50 random ops, verify no unhandled exceptions."""
        monkeypatch.setattr("aiorak._connection.DEFAULT_TIMEOUT", 3.0)

        async def handler(conn: Connection):
            async for data in conn:
                pass

        server = await server_factory(handler=handler, max_connections=10)
        addr = server.local_address

        slots: list[aiorak.Client | None] = [None] * 5
        rng = random.Random(42)
        errors: list[Exception] = []

        for op_num in range(50):
            slot = rng.randint(0, 4)
            action = rng.choice(["connect", "disconnect", "send"])

            try:
                if action == "connect":
                    if slots[slot] is not None:
                        await slots[slot].close()
                    cli = await aiorak.connect(addr, timeout=3.0)
                    slots[slot] = cli

                elif action == "disconnect":
                    if slots[slot] is not None:
                        await slots[slot].close()
                        slots[slot] = None

                elif action == "send":
                    if slots[slot] is not None and slots[slot].is_connected:
                        await slots[slot].send(
                            b"\x86stress" + op_num.to_bytes(2, "big"),
                            reliability=Reliability.RELIABLE_ORDERED,
                        )

            except (RuntimeError, asyncio.TimeoutError, OSError):
                pass
            except Exception as exc:
                errors.append(exc)

        for cli in slots:
            if cli is not None:
                try:
                    await cli.close()
                except Exception:
                    pass

        assert len(errors) == 0, f"Unexpected errors: {errors}"
