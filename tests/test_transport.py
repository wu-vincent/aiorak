"""Tests for _transport.py platform-specific socket options and edge cases."""

import socket
from unittest.mock import MagicMock, patch

from aiorak._transport import RakNetTransport


class TestConnectionMade:
    """Socket option configuration during connection_made."""

    def _make_protocol(self):
        return RakNetTransport(on_datagram=lambda data, addr: None)

    def test_connection_made_no_socket(self):
        """transport.get_extra_info('socket') returning None causes early return without error."""
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_transport.get_extra_info.return_value = None
        proto.connection_made(mock_transport)

    def test_connection_made_rcvbuf_oserror(self):
        """OSError on SO_RCVBUF setsockopt is caught silently."""
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_sock = MagicMock()

        def setsockopt_side_effect(level, optname, value):
            if optname == socket.SO_RCVBUF:
                raise OSError("permission denied")

        mock_sock.setsockopt = MagicMock(side_effect=setsockopt_side_effect)
        mock_transport.get_extra_info.return_value = mock_sock
        proto.connection_made(mock_transport)

    def test_connection_made_broadcast_oserror(self):
        """OSError on SO_BROADCAST setsockopt is caught silently."""
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_sock = MagicMock()

        def setsockopt_side_effect(level, optname, value):
            if optname == socket.SO_BROADCAST:
                raise OSError("not supported")

        mock_sock.setsockopt = MagicMock(side_effect=setsockopt_side_effect)
        mock_transport.get_extra_info.return_value = mock_sock
        proto.connection_made(mock_transport)

    @patch("aiorak._transport.sys")
    def test_connection_made_windows_dontfragment(self, mock_sys):
        """On win32, IP_DONTFRAGMENT socket option is set."""
        mock_sys.platform = "win32"
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_sock = MagicMock()
        mock_transport.get_extra_info.return_value = mock_sock
        proto.connection_made(mock_transport)
        calls = mock_sock.setsockopt.call_args_list
        ip_calls = [c for c in calls if c[0][0] == socket.IPPROTO_IP]
        assert len(ip_calls) >= 1

    @patch("aiorak._transport.sys")
    def test_connection_made_linux_mtu_discover(self, mock_sys):
        """On linux, IP_MTU_DISCOVER socket option is set."""
        mock_sys.platform = "linux"
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_sock = MagicMock()
        mock_transport.get_extra_info.return_value = mock_sock
        proto.connection_made(mock_transport)
        calls = mock_sock.setsockopt.call_args_list
        ip_calls = [c for c in calls if c[0][0] == socket.IPPROTO_IP]
        assert len(ip_calls) >= 1

    @patch("aiorak._transport.sys")
    def test_connection_made_darwin_dontfrag(self, mock_sys):
        """On darwin, IP_DONTFRAG socket option is set."""
        mock_sys.platform = "darwin"
        proto = self._make_protocol()
        mock_transport = MagicMock()
        mock_sock = MagicMock()
        mock_transport.get_extra_info.return_value = mock_sock
        proto.connection_made(mock_transport)
        calls = mock_sock.setsockopt.call_args_list
        ip_calls = [c for c in calls if c[0][0] == socket.IPPROTO_IP]
        assert len(ip_calls) >= 1

    @patch("aiorak._transport.sys")
    def test_platform_specific_oserror(self, mock_sys):
        """Platform-specific IPPROTO_IP OSError is caught silently on each platform."""
        for platform in ("win32", "linux", "darwin"):
            mock_sys.platform = platform
            proto = self._make_protocol()
            mock_transport = MagicMock()
            mock_sock = MagicMock()

            def setsockopt_side_effect(level, optname, value):
                if level == socket.IPPROTO_IP:
                    raise OSError("not supported")

            mock_sock.setsockopt = MagicMock(side_effect=setsockopt_side_effect)
            mock_transport.get_extra_info.return_value = mock_sock
            proto.connection_made(mock_transport)
