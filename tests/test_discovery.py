import queue
import socket
import time
import unittest
from unittest import mock

from privatepartynet import discovery
from privatepartynet.discovery import Discovery, broadcast_targets, clean_name


def wait_until(test, seconds=6.0):
    """Poll a condition for a bounded time. Returns whether it became true."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return True
        time.sleep(0.05)
    return test()


def next_event(instance, kind, seconds=6.0):
    """Return the data of the next event of this kind, skipping others. None when it does not come in time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            found, data = instance.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if found == kind:
            return data
    return None


def peer_named(instance, name):
    return next((peer for peer in instance.peer_list() if peer["name"] == name), None)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Port 0 lets the OS pick free ports. Targets are set after start, once the ports are known, so nothing
        # is broadcast onto a real network during the tests.
        self.a = Discovery("Desk PC", port=0, targets=[], beacon_seconds=0.3)
        self.b = Discovery("Laptop", port=0, targets=[], beacon_seconds=0.3)
        self.a.start()
        self.b.start()
        self.a.targets = [("127.0.0.1", self.b.port)]
        self.b.targets = [("127.0.0.1", self.a.port)]
        self.addCleanup(self.a.stop)
        self.addCleanup(self.b.stop)

    def test_each_side_sees_the_other_by_name(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.assertTrue(wait_until(lambda: peer_named(self.b, "Desk PC") is not None))

    def test_a_name_edit_reaches_the_other_side_at_once(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.b.set_name("Living room")
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Living room") is not None, seconds=1.5))
        self.assertIsNone(peer_named(self.a, "Laptop"))

    def test_a_sharing_change_is_visible_to_others(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.b.set_sharing(8765, "two-way")
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop")["sharing"], seconds=1.5))
        peer = peer_named(self.a, "Laptop")
        self.assertEqual((peer["port"], peer["mode"]), (8765, "two-way"))
        self.b.set_sharing(None, None)
        self.assertTrue(wait_until(lambda: not peer_named(self.a, "Laptop")["sharing"], seconds=1.5))

    def test_an_open_party_names_both_of_its_certificate_pins(self):
        whole, spki = "ab" * 32, "cd" * 32
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.b.set_sharing(8765, "party", whole, spki)
        self.assertTrue(wait_until(lambda: (peer_named(self.a, "Laptop") or {}).get("spki") == spki, seconds=1.5))
        self.assertEqual(peer_named(self.a, "Laptop")["fp"], whole, "the field 3.0/3.1 read is unchanged")
        self.b.set_sharing(8765, "party", whole, "not a pin")
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop")["spki"] is None, seconds=1.5))

    def test_a_request_is_answered_with_the_address_to_open(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.assertTrue(self.a.request(peer_named(self.a, "Laptop")["id"]))
        asked = next_event(self.b, "request")
        self.assertEqual(asked["name"], "Desk PC")
        self.assertTrue(self.b.answer(asked["id"], True, url="https://127.0.0.1:8765/"))
        accepted = next_event(self.a, "accepted")
        self.assertEqual(accepted["url"], "https://127.0.0.1:8765/")
        self.assertEqual(accepted["name"], "Laptop")
        self.assertFalse(accepted["embed"], "an answer that does not say so opens in a window of its own")

    def test_an_accept_says_when_the_room_opens_inside_the_one_window(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.assertTrue(self.a.request(peer_named(self.a, "Laptop")["id"]))
        asked = next_event(self.b, "request")
        self.assertTrue(self.b.answer(asked["id"], True, url="https://127.0.0.1:8765/", embed=True))
        self.assertTrue(next_event(self.a, "accepted")["embed"])

    def test_a_declined_request_says_so(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.a.request(peer_named(self.a, "Laptop")["id"])
        asked = next_event(self.b, "request")
        self.b.answer(asked["id"], False)
        self.assertEqual(next_event(self.a, "declined")["name"], "Laptop")

    def test_a_request_is_never_answered_on_its_own(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.a.request(peer_named(self.a, "Laptop")["id"])
        self.assertIsNotNone(next_event(self.b, "request"))
        self.assertIsNone(next_event(self.a, "accepted", seconds=0.8))

    def test_a_request_to_an_unknown_peer_is_refused(self):
        self.assertFalse(self.a.request("nobody-here"))

    def test_a_request_times_out_without_an_answer(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        with mock.patch.object(discovery, "REQUEST_TIMEOUT", 0.5):
            self.a.request(peer_named(self.a, "Laptop")["id"])
            self.assertIsNotNone(next_event(self.a, "timeout", seconds=5))

    def test_a_goodbye_removes_the_peer_at_once(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        self.b.stop()
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is None, seconds=2))

    def test_a_silent_peer_drops_off_the_list(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        with mock.patch.object(discovery, "STALE_SECONDS", 1.0):
            self.b.targets = []  # b keeps running but stops announcing itself
            self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is None, seconds=6))

    def test_junk_datagrams_are_ignored(self):
        self.assertTrue(wait_until(lambda: peer_named(self.a, "Laptop") is not None))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        for junk in (b"not json", b"[1, 2]", b'{"tag": "other", "kind": "beacon", "id": "x", "name": "Bad"}',
                     b'{"tag": "privatepartynet", "kind": "beacon", "id": "", "name": "Bad"}'):
            sender.sendto(junk, ("127.0.0.1", self.a.port))
        time.sleep(0.3)
        self.assertIsNone(peer_named(self.a, "Bad"))
        self.assertIsNotNone(peer_named(self.a, "Laptop"))  # still healthy after the junk


class NameAndTargetTests(unittest.TestCase):
    def test_names_are_printable_trimmed_and_limited(self):
        self.assertEqual(clean_name("  Desk\x00 PC  "), "Desk PC")
        self.assertEqual(len(clean_name("x" * 100)), discovery.MAX_NAME)

    def test_a_blank_name_falls_back_to_this_pc(self):
        self.assertEqual(clean_name("   "), socket.gethostname()[: discovery.MAX_NAME])

    def test_broadcast_targets_include_the_limited_broadcast_address(self):
        targets = broadcast_targets(8766)
        self.assertIn(("255.255.255.255", 8766), targets)
        self.assertTrue(all(port == 8766 for _, port in targets))


if __name__ == "__main__":
    unittest.main()


class IdentityBeaconTests(DiscoveryTests):
    """A beacon can carry who a PC is (its permanent id, its public keys and its direct message port)."""

    def test_the_announced_identity_is_listed_with_the_peer(self):
        from privatepartynet.identity import Identity

        me = Identity.create()
        self.a.set_identity(me.user_id, me.sign_public.hex(), me.dh_public.hex(), 4321)
        self.assertTrue(wait_until(lambda: (peer_named(self.b, "Desk PC") or {}).get("user") == me.user_id))
        peer = peer_named(self.b, "Desk PC")
        self.assertEqual((peer["sign"], peer["dh"], peer["dm"]), (me.sign_public.hex(), me.dh_public.hex(), 4321))

    def test_a_peer_without_an_identity_has_none(self):
        self.assertTrue(wait_until(lambda: peer_named(self.b, "Desk PC") is not None))
        self.assertEqual(peer_named(self.b, "Desk PC")["user"], None)

    def test_an_id_that_is_not_the_hash_of_its_keys_is_ignored(self):
        from privatepartynet.identity import Identity

        me, other = Identity.create(), Identity.create()
        self.a.set_identity(other.user_id, me.sign_public.hex(), me.dh_public.hex(), 4321)  # claims someone else's id
        self.assertTrue(wait_until(lambda: peer_named(self.b, "Desk PC") is not None))
        time.sleep(0.8)
        peer = peer_named(self.b, "Desk PC")
        self.assertEqual((peer["user"], peer["sign"], peer["dm"]), (None, None, None))

    def test_a_silly_port_is_dropped_but_the_person_is_still_known(self):
        from privatepartynet.identity import Identity

        me = Identity.create()
        self.a.set_identity(me.user_id, me.sign_public.hex(), me.dh_public.hex(), 99999)
        self.assertTrue(wait_until(lambda: (peer_named(self.b, "Desk PC") or {}).get("user") == me.user_id))
        self.assertIsNone(peer_named(self.b, "Desk PC")["dm"])
