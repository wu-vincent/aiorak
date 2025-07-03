import pytest

from aiorak.client import connect


@pytest.mark.asyncio
async def test_connect_incompatible_protocol():
    with pytest.raises(ConnectionRefusedError) as exc_info:
        await connect("test.endstone.dev", 19132, protocol_version=10)

    assert "Incompatible protocol version" in str(exc_info.value)
