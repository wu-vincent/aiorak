"""Port of ComprehensiveTest.cpp - stress test with random operations."""

import asyncio
import os
import random

import pytest

import aiorak


def force_close_transport(target):
    """Close the underlying UDP transport without graceful disconnect."""
    target._socket._transport.close()


pytestmark = pytest.mark.asyncio


@pytest.mark.slow
async def test_comprehensive_stress(server_factory):
    """Randomly connect, send, disconnect, and ping for 5 seconds.

    This is a stability / fuzz-style test: it passes as long as no unhandled
    exceptions bubble up.
    """
    num_slots = 10

    server = await server_factory(max_connections=num_slots)
    addr = server.local_address

    # Pool of client slots - None means "not connected"
    clients: list[aiorak.Client | None] = [None] * num_slots

    # Background tasks collecting recv data (so send buffers don't back up)
    drain_tasks: list[asyncio.Task | None] = [None] * num_slots

    async def _drain(client: aiorak.Client):
        """Silently consume all incoming data until disconnected."""
        try:
            async for _data in client:
                pass
        except Exception:
            pass

    deadline = asyncio.get_event_loop().time() + 5.0
    errors: list[Exception] = []

    while asyncio.get_event_loop().time() < deadline:
        roll = random.random()

        try:
            if roll < 0.10:
                # --- Connect a disconnected client (10%) ---
                disconnected = [i for i in range(num_slots) if clients[i] is None]
                if disconnected:
                    idx = random.choice(disconnected)
                    try:
                        cli = await aiorak.connect(addr, timeout=3.0)
                        clients[idx] = cli
                        drain_tasks[idx] = asyncio.create_task(_drain(cli))
                    except (asyncio.TimeoutError, OSError):
                        pass

            elif roll < 0.30:
                # --- Send random data from a connected client (20%) ---
                connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                if connected:
                    idx = random.choice(connected)
                    size = random.randint(1, 512)
                    data = os.urandom(size)
                    try:
                        await clients[idx].send(
                            data,
                            reliability=aiorak.Reliability.RELIABLE_ORDERED,
                        )
                    except Exception:
                        pass  # client may have disconnected between check and send

            elif roll < 0.40:
                # --- Gracefully close a connected client (10%) ---
                connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                if connected:
                    idx = random.choice(connected)
                    try:
                        await clients[idx].close()
                    except Exception:
                        pass
                    if drain_tasks[idx] is not None:
                        drain_tasks[idx].cancel()
                        drain_tasks[idx] = None
                    clients[idx] = None

            elif roll < 0.45:
                # --- Force-close a connected client (5%) ---
                connected = [i for i in range(num_slots) if clients[i] is not None and clients[i].is_connected]
                if connected:
                    idx = random.choice(connected)
                    try:
                        force_close_transport(clients[idx])
                    except Exception:
                        pass
                    if drain_tasks[idx] is not None:
                        drain_tasks[idx].cancel()
                        drain_tasks[idx] = None
                    clients[idx] = None

            elif roll < 0.50:
                # --- Offline ping (5%) ---
                try:
                    await aiorak.ping(addr, timeout=1.0)
                except (asyncio.TimeoutError, OSError):
                    pass

            else:
                # --- Do nothing / sleep briefly (50%) ---
                await asyncio.sleep(random.uniform(0.01, 0.05))
                continue

        except Exception as exc:
            errors.append(exc)

        # Small sleep to yield control between operations
        await asyncio.sleep(0.01)

    # Cleanup: close all remaining clients
    for i in range(num_slots):
        if drain_tasks[i] is not None:
            drain_tasks[i].cancel()
        if clients[i] is not None:
            try:
                await clients[i].close()
            except Exception:
                pass

    # Allow a moment for cleanup to settle
    await asyncio.sleep(0.1)

    assert not errors, f"Caught {len(errors)} unexpected errors: {errors}"
