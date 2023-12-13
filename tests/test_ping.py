import asyncio
import pytest

from aiorak import RakPeer, ConnectionState





@pytest.fixture
async def sender_one() -> RakPeer:
    sender = RakPeer()
    await sender.start(max_connections=1)
    return sender


@pytest.fixture
async def sender_two() -> RakPeer:
    sender = RakPeer()
    await sender.start(max_connections=1)
    return sender


@pytest.fixture
async def receiver() -> RakPeer:
    receiver = RakPeer()
    await receiver.start(max_connections=2, local_addr=('127.0.0.1', 6000))
    return receiver


@pytest.mark.asyncio
async def test_connect_sender_two_with_receiver(sender_two: RakPeer, receiver: RakPeer) -> None:
    timeout = 5

    try:
        await asyncio.wait_for(wait_and_connect(sender_two, receiver), timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"Connection did not establish within {timeout} seconds")

    assert sender_two.get_connection_state(receiver) == ConnectionState.CONNECTED
