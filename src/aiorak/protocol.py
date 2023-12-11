from asyncio import BaseProtocol, DatagramProtocol


class RakNetProtocol(DatagramProtocol):
    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        print(f"connection_made: {transport}")

    def datagram_received(self, data, addr):
        print(f"datagram_received: {data.decode()} from {addr}")

    def error_received(self, exc):
        print(f"error_received: {exc}")

    def connection_lost(self, exc):
        print(f"connection_lost: {exc}")
