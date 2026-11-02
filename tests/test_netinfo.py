import socket
import unittest

from privatepartynet.netinfo import lan_addresses


class LanAddressTests(unittest.TestCase):
    def test_returns_dotted_ipv4_addresses(self):
        addresses = lan_addresses()
        self.assertTrue(addresses)
        for address in addresses:
            socket.inet_aton(address)  # raises OSError when the text is not an IPv4 address
            self.assertEqual(address.count("."), 3, address)


if __name__ == "__main__":
    unittest.main()
