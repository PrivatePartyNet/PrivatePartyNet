"""The game network end to end over real TLS on this PC: a party, its router, and members' links."""

import ipaddress
import queue
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet.cert import ensure_certificate
from privatepartynet.gamenet import make_packet, parse
from privatepartynet.identity import Identity
from privatepartynet.netlink import NetLink
from privatepartynet.server import Party


class GameNetworkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.party = Party("428617", 0, certfile, keyfile, party_name="Heroes night", host_name="Host",
                           failed_login_delay=0, heartbeat=0.2, server_id="feedfacecafebeef")
        self.party.start()
        self.addCleanup(self.party.stop)

    def member(self, name, role="member"):
        """A person who joined the party (their party window is open, so they are online) and their identity."""
        identity = Identity.create()
        session = self.party.redeem(self.party.join_token(name, role=role, user=identity.user_id))
        member_id = self.party.member_for(session)
        self.party.attach(member_id)  # what opening the party window does
        return identity, member_id

    def link(self, identity, keepalive=15.0):
        received = queue.Queue()
        link = NetLink("127.0.0.1", self.party.port, self.party.fingerprint, identity, received.put, keepalive=keepalive)
        link.start()
        self.addCleanup(link.stop)
        return link, received

    def wait(self, link, state, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and link.status["state"] != state:
            time.sleep(0.05)
        return link.status["state"] == state

    def test_two_members_and_the_host_reach_each_other(self):
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        alex_link, alex_in = self.link(alex)
        sam_link, sam_in = self.link(sam)
        self.assertTrue(self.wait(alex_link, "connected"))
        self.assertTrue(self.wait(sam_link, "connected"))
        network = ipaddress.ip_network(alex_link.status["network"])
        self.assertEqual(sam_link.status["network"], str(network))
        self.assertTrue(network.subnet_of(ipaddress.ip_network("10.0.0.0/8")))
        a, s = alex_link.status["address"], sam_link.status["address"]
        self.assertNotEqual(a, s)
        host_address, host_in, host_send = self.party.net_host()
        self.assertEqual(str(host_address), alex_link.status["host"])

        alex_link.send(make_packet(a, s, b"your move"))
        self.assertEqual(parse(sam_in.get(timeout=3))[0], ipaddress.IPv4Address(a))
        host_send(make_packet(host_address, a, b"from the host"))
        self.assertEqual(alex_in.get(timeout=3)[-13:], b"from the host")
        sam_link.send(make_packet(s, network.broadcast_address, b"anyone hosting?"))
        self.assertEqual(alex_in.get(timeout=3)[-15:], b"anyone hosting?")
        self.assertEqual(host_in.get(timeout=3)[-15:], b"anyone hosting?")

    def test_a_member_keeps_their_address_after_reconnecting_and_after_a_restart(self):
        alex, _ = self.member("Alex")
        first, _ = self.link(alex)
        self.assertTrue(self.wait(first, "connected"))
        address = first.status["address"]
        first.stop()
        again, _ = self.link(alex)
        self.assertTrue(self.wait(again, "connected"))
        self.assertEqual(again.status["address"], address)

    def test_a_forged_sender_does_not_get_through(self):
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        alex_link, _ = self.link(alex)
        sam_link, sam_in = self.link(sam)
        self.assertTrue(self.wait(alex_link, "connected") and self.wait(sam_link, "connected"))
        host_address = alex_link.status["host"]
        alex_link.send(make_packet(host_address, sam_link.status["address"], b"I am the host"))
        with self.assertRaises(queue.Empty):
            sam_in.get(timeout=1)

    def test_a_guest_or_a_stranger_is_refused_for_good(self):
        guest, _ = self.member("Gus", role="guest")
        link, _ = self.link(guest)
        self.assertTrue(self.wait(link, "refused"))
        self.assertIn("Guests", link.status["message"])
        stranger, _ = self.link(Identity.create())
        self.assertTrue(self.wait(stranger, "refused"))

    def test_a_wrong_certificate_is_never_given_the_signature(self):
        alex, _ = self.member("Alex")
        link = NetLink("127.0.0.1", self.party.port, "00" * 32, alex, lambda p: None, max_backoff=0.2)
        link.start()
        self.addCleanup(link.stop)
        time.sleep(1.0)
        self.assertNotEqual(link.status["state"], "connected")
        self.assertIn("certificate differs", link.status["message"])

    def test_being_removed_ends_the_link(self):
        alex, alex_id = self.member("Alex")
        link, _ = self.link(alex)
        self.assertTrue(self.wait(link, "connected"))
        self.party.kick(self.party.host_id, alex_id)
        self.assertTrue(self.wait(link, "refused", seconds=10), link.status)

    def test_becoming_a_guest_ends_the_link(self):
        alex, alex_id = self.member("Alex")
        link, _ = self.link(alex)
        self.assertTrue(self.wait(link, "connected"))
        self.party.set_role(self.party.host_id, alex_id, "guest")
        self.assertTrue(self.wait(link, "refused", seconds=10), link.status)

    def test_an_idle_link_stays_up_on_keepalives(self):
        alex, _ = self.member("Alex")
        link, _ = self.link(alex, keepalive=0.3)
        self.assertTrue(self.wait(link, "connected"))
        time.sleep(1.5)
        self.assertEqual(link.status["state"], "connected")

    def test_the_network_is_kept_with_the_server(self):
        alex, _ = self.member("Alex")
        link, _ = self.link(alex)
        self.assertTrue(self.wait(link, "connected"))
        info = self.party.net_info()
        self.assertEqual(info["network"], link.status["network"])


if __name__ == "__main__":
    unittest.main()
