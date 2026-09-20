from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from terminal.state_machine import get_web_ipv4_address


class GetWebIpv4AddressTests(unittest.TestCase):
    @patch("terminal.state_machine.socket.socket")
    def test_returns_address_selected_by_default_route(self, socket_factory: MagicMock) -> None:
        sock = socket_factory.return_value.__enter__.return_value
        sock.getsockname.return_value = ("192.168.1.23", 43210)

        self.assertEqual(get_web_ipv4_address(), "192.168.1.23")
        sock.connect.assert_called_once_with(("8.8.8.8", 80))

    @patch("terminal.state_machine.socket.socket")
    def test_returns_none_when_network_is_unavailable(self, socket_factory: MagicMock) -> None:
        sock = socket_factory.return_value.__enter__.return_value
        sock.connect.side_effect = OSError("network unreachable")

        self.assertIsNone(get_web_ipv4_address())

    @patch("terminal.state_machine.socket.socket")
    def test_ignores_loopback_address(self, socket_factory: MagicMock) -> None:
        sock = socket_factory.return_value.__enter__.return_value
        sock.getsockname.return_value = ("127.0.0.1", 43210)

        self.assertIsNone(get_web_ipv4_address())


if __name__ == "__main__":
    unittest.main()
