"""The game network's links to the host over real TLS on this PC: a party, its directory, and members' links that follow
the signed list of who is on."""

import ipaddress
import queue
import tempfile
import time
import unittest

from privatepartynet.cert import ensure_certificate
from privatepartynet.gamenet import new_private_key, public_key
from privatepartynet.identity import Identity
from privatepartynet.netlink import NetLink
from privatepartynet.server import Party


class GameNetworkLinkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.owner = Identity.create()
        self.party = Party("428617", 0, certfile, keyfile, party_name="Heroes night", host_name="Host",
                           failed_login_delay=0, heartbeat=0.2, server_id="feedfacecafebeef",
                           host_user_id=self.owner.user_id, owner=self.owner)
        self.party.start()
        self.addCleanup(self.party.stop)

    def member(self, name, role="member"):
        """A person who joined the party (their party window is open, so they are online) and their identity."""
        identity = Identity.create()
        session = self.party.redeem(self.party.join_token(name, role=role, user=identity.user_id))
        member_id = self.party.member_for(session)
        self.party.attach(member_id)  # what opening the party window does
        return identity, member_id

    def link(self, identity, candidates=(), host_sign=None, key=None, fingerprint=None):
        lists = queue.Queue()
        link = NetLink("127.0.0.1", self.party.port, fingerprint or self.party.fingerprint, identity,
                       key or public_key(new_private_key()), lambda roster, relay: lists.put((roster, relay)),
                       candidates=candidates, server_id=self.party.server_id, max_backoff=0.2,
                       host_sign=self.owner.sign_public.hex() if host_sign is None else host_sign)
        link.start()
        self.addCleanup(link.stop)
        return link, lists

    def wait(self, link, state, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and link.status["state"] != state:
            time.sleep(0.05)
        return link.status["state"] == state

    def newest(self, lists, until, seconds=8):
        """The newest list once `until(list)` holds."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                roster, relay = lists.get(timeout=0.2)
            except queue.Empty:
                continue
            if until(roster):
                return roster
        self.fail("the list never came")

    def users(self, roster):
        return sorted(p["user"] for p in roster["peers"])

    def test_every_member_gets_the_whole_signed_list_and_the_host_is_dot_one(self):
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        host_key = public_key(new_private_key())
        self.assertEqual(str(self.party.net_host(host_key, [["lan", "192.168.1.2", 4000]])), self.party.net_info()["host"])
        alex_link, alex_lists = self.link(alex, candidates=[["lan", "192.168.1.3", 4001]])
        sam_link, sam_lists = self.link(sam)
        self.assertTrue(self.wait(alex_link, "connected") and self.wait(sam_link, "connected"))
        network = ipaddress.ip_network(alex_link.status["network"])
        self.assertTrue(network.subnet_of(ipaddress.ip_network("10.0.0.0/8")))
        everyone = sorted([self.owner.user_id, alex.user_id, sam.user_id])
        roster = self.newest(alex_lists, lambda r: self.users(r) == everyone)
        by_user = {p["user"]: p for p in roster["peers"]}
        self.assertEqual(by_user[self.owner.user_id]["address"], alex_link.status["host"])
        self.assertEqual(by_user[self.owner.user_id]["key"], host_key)
        self.assertEqual(by_user[alex.user_id]["address"], alex_link.status["address"])
        self.assertEqual(by_user[alex.user_id]["candidates"], [["lan", "192.168.1.3", 4001]])
        self.newest(sam_lists, lambda r: self.users(r) == everyone)

    def test_each_member_gets_their_own_relay_settings_on_their_own_link_and_fresh_ones_in_time(self):
        from privatepartynet import gamenet

        asked = []

        def path_relay_for(user, sign_public=None):
            asked.append((user, sign_public))
            return {"for": user, "n": len(asked)}

        self.party.path_relay_for = path_relay_for
        saved = gamenet.RELAY_REFRESH_SECONDS
        gamenet.RELAY_REFRESH_SECONDS = 0.5
        self.addCleanup(setattr, gamenet, "RELAY_REFRESH_SECONDS", saved)
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        relays = queue.Queue()
        link = NetLink("127.0.0.1", self.party.port, self.party.fingerprint, alex, public_key(new_private_key()),
                       lambda roster, relay: relays.put(relay), server_id=self.party.server_id,
                       host_sign=self.owner.sign_public.hex())
        link.start()
        self.addCleanup(link.stop)
        sam_link, _ = self.link(sam)
        first = relays.get(timeout=8)
        self.assertEqual(first["for"], alex.user_id, "only Alex's own settings on Alex's link")
        self.assertIn((alex.user_id, alex.sign_public.hex()), asked, "bound to Alex's signing key")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            again = relays.get(timeout=8)
            if again["n"] != first["n"] and again["for"] == alex.user_id:
                break
        else:
            self.fail("no fresh settings came")

    def test_new_ways_to_reach_a_member_reach_everyone(self):
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        alex_link, _ = self.link(alex)
        sam_link, sam_lists = self.link(sam)
        self.assertTrue(self.wait(alex_link, "connected") and self.wait(sam_link, "connected"))
        self.assertTrue(alex_link.set_candidates([["upnp", "203.0.113.7", 5000]]))
        self.newest(sam_lists, lambda r: any(p["candidates"] == [["upnp", "203.0.113.7", 5000]] for p in r["peers"]))

    def test_a_removed_or_banned_member_is_dropped_from_everyones_list_at_once(self):
        alex, alex_id = self.member("Alex")
        sam, sam_id = self.member("Sam")
        gus, _ = self.member("Gus")
        alex_link, alex_lists = self.link(alex)
        sam_link, _ = self.link(sam)
        gus_link, _ = self.link(gus)
        self.assertTrue(all(self.wait(link, "connected") for link in (alex_link, sam_link, gus_link)))
        self.newest(alex_lists, lambda r: len(r["peers"]) == 3)
        started = time.monotonic()
        self.party.ban(self.party.host_id, sam_id)
        self.newest(alex_lists, lambda r: sam.user_id not in self.users(r))
        self.assertLess(time.monotonic() - started, 2.0, "at once, not at the next keep-alive")
        self.assertTrue(self.wait(sam_link, "refused", seconds=10), sam_link.status)
        self.party.set_role(self.party.host_id, alex_id, "guest")
        self.assertTrue(self.wait(alex_link, "refused", seconds=10), alex_link.status)

    def test_closing_a_link_or_the_party_window_takes_the_member_off(self):
        alex, alex_id = self.member("Alex")
        sam, _ = self.member("Sam")
        alex_link, _ = self.link(alex)
        sam_link, sam_lists = self.link(sam)
        self.assertTrue(self.wait(alex_link, "connected") and self.wait(sam_link, "connected"))
        self.newest(sam_lists, lambda r: alex.user_id in self.users(r))
        alex_link.stop()
        self.newest(sam_lists, lambda r: alex.user_id not in self.users(r))

    def test_a_member_keeps_their_address_after_reconnecting(self):
        alex, _ = self.member("Alex")
        first, _ = self.link(alex)
        self.assertTrue(self.wait(first, "connected"))
        address = first.status["address"]
        first.stop()
        again, _ = self.link(alex)
        self.assertTrue(self.wait(again, "connected"))
        self.assertEqual(again.status["address"], address)

    def test_someone_elses_wireguard_key_is_refused(self):
        alex, _ = self.member("Alex")
        sam, _ = self.member("Sam")
        key = public_key(new_private_key())
        alex_link, _ = self.link(alex, key=key)
        self.assertTrue(self.wait(alex_link, "connected"))
        sam_link, _ = self.link(sam, key=key)
        self.assertTrue(self.wait(sam_link, "retrying"))
        self.assertIn("409", sam_link.status["message"])

    def test_a_list_not_signed_by_the_host_is_not_followed(self):
        alex, _ = self.member("Alex")
        link, lists = self.link(alex, host_sign=Identity.create().sign_public.hex())
        self.assertTrue(self.wait(link, "retrying"))
        self.assertIn("not signed by the host", link.status["message"])
        self.assertTrue(lists.empty())

    def test_a_guest_or_a_stranger_is_refused_for_good(self):
        guest, _ = self.member("Gus", role="guest")
        link, _ = self.link(guest)
        self.assertTrue(self.wait(link, "refused"))
        self.assertIn("Guests", link.status["message"])
        stranger, _ = self.link(Identity.create())
        self.assertTrue(self.wait(stranger, "refused"))

    def test_a_wrong_certificate_is_never_given_the_signature(self):
        alex, _ = self.member("Alex")
        link, _ = self.link(alex, fingerprint="00" * 32)
        seen = set()
        deadline = time.monotonic() + 8  # it keeps trying, so look at each attempt as it ends rather than at one moment
        while time.monotonic() < deadline and not any("certificate differs" in m for m in seen):
            seen.add(link.status["message"])
            self.assertNotEqual(link.status["state"], "connected")
            time.sleep(0.02)
        self.assertTrue(any("certificate differs" in m for m in seen), seen)

    def test_an_idle_link_stays_up_on_keepalives(self):
        from privatepartynet import gamenet

        alex, _ = self.member("Alex")
        saved = gamenet.KEEPALIVE_SECONDS
        gamenet.KEEPALIVE_SECONDS = 0.3
        self.addCleanup(setattr, gamenet, "KEEPALIVE_SECONDS", saved)
        link, _ = self.link(alex)
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
