"""Paths between members, on this PC: every rung of the ladder, with software routers (fakenat.py) in place of real ones.
The test that matters most: two members both behind hard routers reach each other through a friend or a relay server."""

import os
import sys
import threading
import time
import unittest
from pathlib import Path

from privatepartynet import relayproto as rp
from privatepartynet.identity import Identity
from privatepartynet.pathway import (DATA, MAX_DATAGRAM, PROBE, LoopbackHub, LoopbackPathManager, PathManager,
                                     make_pass, open_relay_config, sign_relay_config)
from fakenat import NatTransport
from test_connect import FakeStun

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
from ppn_relay import ThreadedRelay  # noqa: E402

OWNER = Identity.create()  # the party's owner, whose passes friend relays accept


def friend_config(identity):
    """What the owner's PC hands a member for friend relays: the owner's key and a pass for this member."""
    return {"friend": {"owner": OWNER.sign_public.hex(),
                       "pass": make_pass(OWNER.sign, identity.user_id, identity.sign_public).hex()}}


def wait_for(condition, seconds=10.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class Member:
    """One member's PathManager, and what has arrived for it."""

    def __init__(self, test, behind=None, addresses=(), stun=(), relay_config=None, **settings):
        self.identity = Identity.create()
        if callable(relay_config):
            relay_config = relay_config(self.identity)
        transport = NatTransport(symmetric=behind == "hard") if behind else None
        self.manager = PathManager(self.identity, settings, relay_config, transport=transport,
                                   addresses=list(addresses), stun_servers=list(stun), probe_network=False)
        test.addCleanup(self.manager.stop)
        self.got = []
        self.changes = []
        self.paths = {}

    @property
    def id(self):
        return self.identity.user_id

    def path(self, other):
        path = self.manager.path_to(other.id, other.manager.local_candidates())
        path.set_receiver(self.got.append)
        path.set_on_change(self.changes.append)
        self.paths[other.id] = path
        return path


def mesh(*members):
    """Every member learns every other's candidates now and whenever they change, as the party passes them on."""
    for member in members:
        def changed(_, me=member):
            for other in members:
                if other is not me:
                    other.manager.path_to(me.id, me.manager.local_candidates())

        member.manager.set_on_candidates(changed)
    for member in members:
        for other in members:
            if other is not member:
                member.path(other)


class Base(unittest.TestCase):
    def stuns(self):
        fakes = [FakeStun(), FakeStun()]
        for fake in fakes:
            self.addCleanup(fake.close)
        return [fake.address for fake in fakes]

    def relay_server(self, secret="group secret", **options):
        relay = ThreadedRelay([secret], host="127.0.0.1", udp_port=0, **options)
        self.addCleanup(relay.close)
        return relay

    def vps_config(self, relay, member_id, secret="group secret"):
        return {"vps": [{"host": "127.0.0.1", "port": relay.udp_port, "tls_port": 0, "pin": "",
                         "credential": rp.member_credential(secret, member_id).hex()}]}

    def exchange(self, a, b, payload=b"wireguard ciphertext"):
        """A datagram each way arrives."""
        a.paths[b.id].send(payload + b" from a")
        b.paths[a.id].send(payload + b" from b")
        self.assertTrue(wait_for(lambda: payload + b" from a" in b.got and payload + b" from b" in a.got, 5),
                        (a.got[-3:], b.got[-3:]))


class LadderTests(Base):
    def test_two_members_on_one_network_use_lan(self):
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "lan" and b.paths[a.id].kind == "lan"))
        self.exchange(a, b)
        self.assertEqual(a.changes[0], "lan")

    def test_the_way_in_use_reports_its_round_trip_for_the_game_networks_latency(self):
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "lan"))
        self.assertTrue(wait_for(lambda: a.manager.state()["peers"][b.id]["rtt"] is not None))
        seen = a.manager.state()["peers"][b.id]
        self.assertEqual(seen["kind"], "lan")
        self.assertIsInstance(seen["rtt"], int, "whole milliseconds, from the probes the paths send anyway")
        self.assertTrue(0 <= seen["rtt"] < 500, seen)

    def test_two_easy_routers_punch_through(self):
        stun = self.stuns()
        a, b = Member(self, behind="easy", stun=stun), Member(self, behind="easy", stun=stun)
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.manager.public and b.manager.public))
        self.assertEqual(a.manager.public[2], "easy")
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "punch" and b.paths[a.id].kind == "punch", 15))
        self.exchange(a, b)

    def test_two_hard_routers_meet_through_a_friend_who_turned_relaying_on(self):
        stun = self.stuns()
        a = Member(self, behind="hard", stun=stun, relay_config=friend_config)
        b = Member(self, behind="hard", stun=stun, relay_config=friend_config)
        friend = Member(self, addresses=[("upnp", "127.0.0.1")], relay_config=friend_config, friend_relay=True)
        mesh(a, b, friend)
        self.assertTrue(wait_for(lambda: a.manager.public and a.manager.public[2] == "hard"))
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "friend" and b.paths[a.id].kind == "friend", 15),
                        (a.manager.state(), b.manager.state()))
        self.exchange(a, b)
        # Each of them reaches the friend directly: its port is open.
        self.assertEqual(a.paths[friend.id].kind, "upnp")
        self.assertGreater(friend.manager.state()["relayed"]["forwarded"], 0)
        # The friend forwarded only what the pair sent; nothing it handled could be read as theirs.
        self.assertNotIn(b"wireguard ciphertext from a", friend.got)
        self.assertEqual(friend.manager.state()["through"], sorted([a.id, b.id]), "the friend can see whom it carries")
        self.assertEqual(a.manager.friend_offers(), [("127.0.0.1", friend.manager.port)], "for the invites")
        self.assertEqual(a.manager.problems(), [])

    def test_two_hard_routers_meet_through_the_groups_relay_server(self):
        stun = self.stuns()
        relay = self.relay_server()
        a = Member(self, behind="hard", stun=stun)
        b = Member(self, behind="hard", stun=stun)
        a.manager.set_relay_config(self.vps_config(relay, a.id))
        b.manager.set_relay_config(self.vps_config(relay, b.id))
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "vps" and b.paths[a.id].kind == "vps", 15),
                        (a.manager.state(), b.manager.state()))
        self.exchange(a, b)
        big = os.urandom(MAX_DATAGRAM)
        a.paths[b.id].send(big)
        self.assertTrue(wait_for(lambda: big in b.got, 5))
        self.assertGreater(relay.server.core.stats["forwarded"], 0)

    def test_two_hard_routers_with_no_relay_find_no_way_and_say_why(self):
        from unittest import mock

        from privatepartynet import pathway

        stun = self.stuns()
        with mock.patch.object(pathway, "FAST_SECONDS", 2.0):
            a, b = Member(self, behind="hard", stun=stun), Member(self, behind="hard", stun=stun)
            mesh(a, b)
            self.assertTrue(wait_for(lambda: a.manager.problems(), 10))
            (problem,) = a.manager.problems()
        self.assertEqual(a.paths[b.id].kind, "")
        a.paths[b.id].send(b"nowhere to go")
        self.assertEqual(a.paths[b.id].stats["dropped"], 1)
        self.assertEqual(problem["peer"], b.id)
        self.assertIn("new port for every connection", problem["why"])
        self.assertIn("relaying for others", problem["why"])
        self.assertIn("relay server", problem["why"])

    def test_relaying_for_others_is_off_unless_turned_on(self):
        stun = self.stuns()
        a = Member(self, behind="hard", stun=stun, relay_config=friend_config)
        b = Member(self, behind="hard", stun=stun, relay_config=friend_config)
        friend = Member(self, addresses=[("upnp", "127.0.0.1")], relay_config=friend_config)  # no setting given
        self.assertFalse([c for c in friend.manager.local_candidates() if c[0] == "friend"])
        self.assertFalse(friend.manager.state()["friend_relay"])
        mesh(a, b, friend)
        time.sleep(3)
        self.assertEqual(a.paths[b.id].kind, "")
        self.assertTrue(wait_for(lambda: a.paths[friend.id].kind == "upnp"))
        friend.manager.set_friend_relay(True)  # the friend turns it on: the pair gets through
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "friend", 20), a.manager.state())

    def test_a_relayed_path_moves_to_a_direct_one_when_it_answers(self):
        relay = self.relay_server()
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        a.manager.set_relay_config(self.vps_config(relay, a.id))
        b.manager.set_relay_config(self.vps_config(relay, b.id))
        # At first each knows only the other's keys, so only the relay can carry them.
        keys_only = lambda m: [c for c in m.manager.local_candidates() if c[0] == "key"]  # noqa: E731
        pa = a.manager.path_to(b.id, keys_only(b))
        pb = b.manager.path_to(a.id, keys_only(a))
        pa.set_on_change(a.changes.append)
        pb.set_receiver(b.got.append)
        self.assertTrue(wait_for(lambda: pa.kind == "vps" and pb.kind == "vps", 10))
        pa.send(b"over the relay")
        self.assertTrue(wait_for(lambda: b"over the relay" in b.got, 5))
        # Then the full candidates arrive (as after a later punch, or a router port opening): the path moves.
        self.assertIs(a.manager.path_to(b.id, b.manager.local_candidates()), pa)
        b.manager.path_to(a.id, a.manager.local_candidates())
        self.assertTrue(wait_for(lambda: pa.kind == "lan" and pb.kind == "lan", 10))
        self.assertEqual(a.changes, ["vps", "lan"])
        pa.send(b"directly")
        self.assertTrue(wait_for(lambda: b"directly" in b.got, 5))

    def test_a_path_falls_back_to_the_relay_when_the_direct_way_stops(self):
        relay = self.relay_server()
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        a.manager.set_relay_config(self.vps_config(relay, a.id))
        b.manager.set_relay_config(self.vps_config(relay, b.id))
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "lan", 10))
        self.assertTrue(wait_for(lambda: any(r.kind == "vps" and r.alive(time.monotonic())
                                             for r in a.manager._peers[b.id].routes.values()), 10))
        # b's direct address goes quiet: everything a sends there is lost from now on.
        lan_route = a.manager._peers[b.id].active
        lan_route.address = ("127.0.0.1", 9)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "vps", 15))
        self.assertEqual(a.changes[-1], "vps")


class FriendPassTests(Base):
    def test_a_member_with_a_forged_pass_cannot_use_a_friend_relay(self):
        """H2: friend relays accept only passes the owner signed; a member cannot make one for a made-up id."""
        stun = self.stuns()

        def self_made(identity):  # signed with the member's own key instead of the owner's
            return {"friend": {"owner": OWNER.sign_public.hex(),
                               "pass": make_pass(identity.sign, identity.user_id, identity.sign_public).hex()}}

        a = Member(self, behind="hard", stun=stun, relay_config=self_made)
        b = Member(self, behind="hard", stun=stun, relay_config=friend_config)
        friend = Member(self, addresses=[("upnp", "127.0.0.1")], relay_config=friend_config, friend_relay=True)
        mesh(a, b, friend)
        self.assertTrue(wait_for(lambda: a.paths[friend.id].kind == "upnp"))
        time.sleep(3)
        self.assertEqual(a.paths[b.id].kind, "", "the friend refused a's self-made pass")
        self.assertGreater(friend.manager.state()["relayed"]["refused"], 0)


class SafetyTests(Base):
    def test_a_member_whose_keys_do_not_match_their_id_gets_no_path(self):
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        stranger = Identity.create()
        lying = [["key", stranger.dh_public.hex(), stranger.sign_public.hex()]] + \
                [c for c in b.manager.local_candidates() if c[0] != "key"]
        path = a.manager.path_to(b.id, lying)
        b.path(a)
        time.sleep(2)
        self.assertEqual(path.kind, "")

    def test_data_from_an_address_that_never_proved_itself_is_dropped(self):
        import socket

        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "lan"))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(bytes([DATA]) + b"forged", ("127.0.0.1", a.manager.port))
            sock.sendto(bytes([PROBE]) + bytes.fromhex(b.id) + os.urandom(24), ("127.0.0.1", a.manager.port))
        time.sleep(0.5)
        self.assertNotIn(b"forged", a.got)

    def test_the_full_size_probe_is_what_the_game_network_sends_over_the_worst_path(self):
        from privatepartynet import pathway

        self.assertEqual(pathway.FULL_SIZE, pathway.GAME_MAX_DATAGRAM + 1, "the largest WireGuard datagram and its marker")
        self.assertEqual(pathway.FULL_SIZE + rp.HEADER + 40 + 8, 1400, "relayed over IPv6: the 1400-byte budget, exactly")
        self.assertLessEqual(pathway.FULL_SIZE + rp.HEADER, rp.MAX_PAYLOAD + rp.HEADER, "a relay carries it")
        a = Member(self, addresses=[("lan", "127.0.0.1")], max_datagram=1200)
        self.assertEqual(a.manager._full_size, 1201, "the game network may say another size")

    def test_too_big_a_datagram_is_dropped_not_cut(self):
        a, b = Member(self, addresses=[("lan", "127.0.0.1")]), Member(self, addresses=[("lan", "127.0.0.1")])
        mesh(a, b)
        self.assertTrue(wait_for(lambda: a.paths[b.id].kind == "lan"))
        a.paths[b.id].send(b"x" * (MAX_DATAGRAM + 1))
        self.assertEqual(a.paths[b.id].stats["dropped"], 1)

    def test_relay_settings_are_opened_only_with_the_owners_signature(self):
        owner, someone = Identity.create(), Identity.create()
        config = {"vps": [], "expires": 1}
        signed = sign_relay_config(config, owner.sign)
        self.assertEqual(open_relay_config(signed, owner.sign_public.hex()), config)
        self.assertIsNone(open_relay_config(signed, someone.sign_public.hex()))
        signed["relay"]["expires"] = 2
        self.assertIsNone(open_relay_config(signed, owner.sign_public.hex()))

    def test_a_new_address_reaches_the_others(self):
        from unittest import mock

        from privatepartynet import pathway

        addresses = [("ipv6", "2001:db8::5")]
        seen = []
        with mock.patch.object(pathway, "ADDRESS_SECONDS", 0.2):
            a = Member(self, addresses=addresses)
            a.manager._addresses = addresses
            a.manager.set_on_candidates(seen.append)
            addresses[0] = ("ipv6", "2001:db8::6")  # the provider handed out a new prefix
            self.assertTrue(wait_for(lambda: seen and ["ipv6", "2001:db8::6", a.manager.port] in seen[-1], 5))

    def test_candidates_carry_the_keys_and_change_when_stun_answers(self):
        stun = self.stuns()
        seen = []
        a = Member(self, behind="easy", stun=stun)
        a.manager.set_on_candidates(seen.append)
        self.assertEqual(a.manager.local_candidates()[0][0], "key")
        self.assertTrue(wait_for(lambda: any(c[0] == "punch" for c in a.manager.local_candidates())))
        self.assertTrue(seen and any(c[0] == "punch" for c in seen[-1]))


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.hub = LoopbackHub()
        self.addCleanup(self.hub.close)

    def test_two_loopback_managers_carry_datagrams_both_ways(self):
        a = LoopbackPathManager("a" * 32, hub=self.hub)
        b = LoopbackPathManager("b" * 32, hub=self.hub)
        got_a, got_b = [], []
        pa, pb = a.path_to(b.user_id, b.local_candidates()), b.path_to(a.user_id, a.local_candidates())
        pa.set_receiver(got_a.append)
        pb.set_receiver(got_b.append)
        pa.send(b"one")
        pb.send(b"two")
        self.assertTrue(wait_for(lambda: got_b == [b"one"] and got_a == [b"two"], 2))
        self.assertEqual(pa.kind, "lan")

    def test_switch_calls_the_change_handlers(self):
        a = LoopbackPathManager("a" * 32, hub=self.hub)
        b = LoopbackPathManager("b" * 32, hub=self.hub)
        changes = []
        path = a.path_to(b.user_id, [])
        path.set_on_change(changes.append)
        a.switch("vps")
        a.switch("punch", peer_id=b.user_id)
        self.assertEqual(changes, ["vps", "punch"])
        self.assertEqual(path.kind, "punch")

    def test_nothing_arrives_without_a_path_back_or_after_close(self):
        a = LoopbackPathManager("a" * 32, hub=self.hub)
        b = LoopbackPathManager("b" * 32, hub=self.hub)
        got = []
        pa = a.path_to(b.user_id, [])
        pa.send(b"nobody listening")
        self.assertEqual(pa.stats["dropped"], 1)
        pb = b.path_to(a.user_id, [])
        pb.set_receiver(got.append)
        pb.close()
        pa.send(b"closed")
        time.sleep(0.2)
        self.assertEqual(got, [])

    def test_loss_drops_some_datagrams(self):
        a = LoopbackPathManager("a" * 32, hub=self.hub, loss=0.5)
        b = LoopbackPathManager("b" * 32, hub=self.hub)
        got = []
        pa = a.path_to(b.user_id, [])
        b.path_to(a.user_id, []).set_receiver(got.append)
        for _ in range(400):
            pa.send(b"x")
        time.sleep(0.5)
        self.assertTrue(100 < len(got) < 300, len(got))


if __name__ == "__main__":
    unittest.main()
