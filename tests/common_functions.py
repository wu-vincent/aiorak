from aiorak import RakPeer, ConnectionState


async def wait_and_connect(peer_from: RakPeer, peer_to: RakPeer):
    while peer_from.get_connection_state(peer_to) != ConnectionState.CONNECTED:
        if peer_from.get_connection_state(peer_to) not in [ConnectionState.CONNECTED,
                                                           ConnectionState.CONNECTING,
                                                           ConnectionState.PENDING,
                                                           ConnectionState.DISCONNECTING]:
            await peer_from.connect(remote_addr=("127.0.0.1", 6000))

        await asyncio.sleep(0.1)
