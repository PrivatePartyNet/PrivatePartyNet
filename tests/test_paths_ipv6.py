"""Paths between members over IPv6, with pathway.PathManager put through natsim's routers and firewalls.

A game's IPv4 packets on the game network are sealed by WireGuard, and the datagrams that carry them may travel over
IPv6. IPv6 has no carrier-grade NAT, only firewalls, so two members who both have IPv6 reach each other directly even
when both are behind symmetric CGNAT on IPv4. These tests prove that, and the cases around it: a relay reached on
whichever of its addresses answers, and a way that carries small packets but not large ones left for one that does.

They need pathway.py and the relay server, and are skipped where those are not in the tree yet."""

import os
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import natsim
from privatepartynet.identity import Identity

try:
    from privatepartynet import relayclient, relayproto as rp
    from privatepartynet.pathway import MAX_DATAGRAM, PathManager, make_pass

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
    from ppn_relay import ThreadedRelay
except ImportError:
    PathManager = None

SECRET = "group secret"
OWNER = Identity.create()  # the party's owner, whose passes friend relays accept


def wait_for(condition, seconds=10.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class Member:
    """One member's PathManager on a natsim PC, and what has arrived for it."""

    def __init__(self, test, v4, v6, relay_config=None, v6_mtu=None, **settings):
        self.identity = Identity.create()
        self.pc = test.internet.pc(v4=v4, v6=v6, v6_mtu=v6_mtu)
        if callable(relay_config):
            relay_config = relay_config(self.identity)
        self.manager = PathManager(self.identity, settings, relay_config, transport=self.pc,
                                   addresses=self.pc.addresses, stun_servers=list(test.stun), probe_network=False)
        test.addCleanup(self.manager.stop)
        self.got = []
        self.paths = {}

    @property
    def id(self):
        return self.identity.user_id


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
                path = member.manager.path_to(other.id, other.manager.local_candidates())
                path.set_receiver(member.got.append)
                member.paths[other.id] = path


@unittest.skipIf(PathManager is None, "pathway.py and the relay server are not in this tree yet")
class Base(unittest.TestCase):
    def setUp(self):
        self.internet = natsim.Internet()
        self.addCleanup(self.internet.close)
        self.stun = self.internet.stun_servers(2)

    def relay(self):
        """A relay server on IPv4 and IPv6 at once: (its IPv4 address, its IPv6 address), both (address, port)."""
        relay = ThreadedRelay([SECRET], host="127.0.0.1", udp_port=0)
        self.addCleanup(relay.close)
        return self.internet.server(relay.udp_port), self.internet.server(relay.udp_port, family=6)

    def vps(self, *addresses):
        """relay_config for one member: the relay at each of `addresses`, with UDP only (no TLS fallback)."""
        def config(identity):
            credential = rp.member_credential(SECRET, identity.user_id).hex()
            return {"vps": [{"host": host, "port": port, "tls_port": 0, "pin": "", "credential": credential}
                            for host, port in addresses]}

        return config

    def kinds(self, a, b):
        return a.paths[b.id].kind, b.paths[a.id].kind

    def connects(self, a, b, kind, seconds=15):
        """Both paths settle on `kind`, and a datagram as big as a path carries arrives each way."""
        self.assertTrue(wait_for(lambda: self.kinds(a, b) == (kind, kind), seconds),
                        (self.kinds(a, b), a.manager.state(), b.manager.state()))
        big = os.urandom(MAX_DATAGRAM - 8)
        a.paths[b.id].send(b"from a" + big[:2])
        b.paths[a.id].send(b"from b" + big[:2])
        self.assertTrue(wait_for(lambda: any(d.startswith(b"from a") for d in b.got) and
                                 any(d.startswith(b"from b") for d in a.got), 5), "a small datagram each way")
        a.paths[b.id].send(b"big" + big[3:])
        self.assertTrue(wait_for(lambda: b"big" + big[3:] in b.got, 5), "a datagram of MAX_DATAGRAM bytes")

    def finds_no_way(self, a, b, seconds=8):
        time.sleep(seconds)
        self.assertEqual(self.kinds(a, b), ("", ""))


class DirectTests(Base):
    def test_cgnat_on_both_sides_and_stateful_ipv6_connect_directly_over_ipv6(self):
        """The case the user asked for: symmetric CGNAT on IPv4 at both ends, a home router's IPv6 firewall at both.
        No relay is given, so only IPv6 can work."""
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL)
        b = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL)
        mesh(a, b)
        self.connects(a, b, "ipv6")
        self.assertEqual((a.manager.public[2], b.manager.public[2]), ("hard", "hard"), "IPv4 alone had no way")

    def test_every_mix_of_open_and_stateful_ipv6_connects_over_ipv6(self):
        for kinds in ((natsim.V6_OPEN, natsim.V6_OPEN), (natsim.V6_OPEN, natsim.V6_STATEFUL),
                      (natsim.V6_STATEFUL, natsim.V6_OPEN)):
            with self.subTest(kinds=kinds):
                a = Member(self, natsim.SYMMETRIC, kinds[0])
                b = Member(self, natsim.SYMMETRIC, kinds[1])
                mesh(a, b)
                self.connects(a, b, "ipv6")

    def test_ipv6_is_used_even_where_the_ipv4_punch_would_work(self):
        a = Member(self, natsim.PORT_RESTRICTED, natsim.V6_STATEFUL)
        b = Member(self, natsim.PORT_RESTRICTED, natsim.V6_STATEFUL)
        mesh(a, b)
        self.connects(a, b, "ipv6")

    def test_ipv6_that_lets_nothing_in_leaves_the_ipv4_punch_to_do_it(self):
        a = Member(self, natsim.PORT_RESTRICTED, natsim.V6_BLOCKS_INBOUND)
        b = Member(self, natsim.PORT_RESTRICTED, natsim.V6_BLOCKS_INBOUND)
        mesh(a, b)
        self.connects(a, b, "punch")

    def test_ipv6_on_one_side_only_is_no_way_through_cgnat(self):
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL)
        b = Member(self, natsim.SYMMETRIC, natsim.NO_V6)
        mesh(a, b)
        self.finds_no_way(a, b)

    def test_one_firewall_that_blocks_inbound_udp_is_no_way_through_cgnat(self):
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL)
        b = Member(self, natsim.SYMMETRIC, natsim.V6_BLOCKS_INBOUND)
        mesh(a, b)
        self.finds_no_way(a, b)


class RelayTests(Base):
    def test_ipv6_on_one_side_only_meets_on_the_relay(self):
        v4, _ = self.relay()
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL, relay_config=self.vps(v4))
        b = Member(self, natsim.SYMMETRIC, natsim.NO_V6, relay_config=self.vps(v4))
        mesh(a, b)
        self.connects(a, b, "vps")

    def test_an_ipv6_only_member_and_an_ipv4_only_member_meet_on_a_dual_stack_relay(self):
        """One member's network carries no IPv4 UDP (only IPv6 works), the other has no IPv6. Only a relay that is on
        both can bridge them; the owner's settings list it at both addresses."""
        v4, v6 = self.relay()
        a = Member(self, natsim.UDP_BLOCKED, natsim.V6_STATEFUL, relay_config=self.vps(v4, v6))
        b = Member(self, natsim.SYMMETRIC, natsim.NO_V6, relay_config=self.vps(v4, v6))
        mesh(a, b)
        self.connects(a, b, "vps")

    def test_a_relay_named_once_reaches_an_ipv6_only_member(self):
        """Today relayclient.resolve() takes the first IPv4 address of a name and never tries its IPv6 one, so a member
        whose network carries no IPv4 UDP cannot reach a relay the owner gave by name (as a VPS usually is). Fix: try
        each address the name has (IPv6 too, as RFC 8305 "happy eyeballs" does), or keep one RelayClient per address
        and use whichever answers."""
        v4, v6 = self.relay()
        real = socket.getaddrinfo

        def lookup(host, port, *args, **kwargs):
            # The name has both an A and an AAAA record. natsim gives the relay's IPv4 side a port of its own, so each
            # answer carries its own port rather than the one asked for.
            if host != "relay.example":
                return real(host, port, *args, **kwargs)
            return [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", (v6[0], v6[1], 0, 0)),
                    (socket.AF_INET, socket.SOCK_DGRAM, 17, "", v4)]

        with mock.patch.object(relayclient.socket, "getaddrinfo", lookup):
            a = Member(self, natsim.UDP_BLOCKED, natsim.V6_STATEFUL, relay_config=self.vps(("relay.example", v6[1])))
            b = Member(self, natsim.SYMMETRIC, natsim.NO_V6, relay_config=self.vps(("relay.example", v6[1])))
            mesh(a, b)
            self.connects(a, b, "vps")

    def test_a_member_on_both_families_of_one_relay_keeps_a_steady_path(self):
        """Both members have IPv4 and IPv6 and the relay is listed at both addresses, so each member binds the same
        channel twice. The path must not stall each time the relay moves a member's end from one family to the other."""
        v4, v6 = self.relay()
        a = Member(self, natsim.SYMMETRIC, natsim.V6_BLOCKS_INBOUND, relay_config=self.vps(v4, v6))
        b = Member(self, natsim.SYMMETRIC, natsim.V6_BLOCKS_INBOUND, relay_config=self.vps(v4, v6))
        mesh(a, b)
        self.connects(a, b, "vps")
        sent, ends = 0, time.monotonic() + 35  # two rebinds (every 15 s) for each member
        while time.monotonic() < ends:
            a.paths[b.id].send(b"tick %d" % sent)
            sent += 1
            time.sleep(0.25)
        time.sleep(1)
        arrived = sum(1 for d in b.got if d.startswith(b"tick "))
        self.assertGreater(arrived, sent * 0.9, f"{arrived} of {sent} arrived")

    def test_a_full_size_datagram_crosses_an_ipv6_path_with_the_smallest_mtu(self):
        """IPv6 links may be as small as 1280 bytes (tunnels, some mobile networks), and IPv6 routers never split a
        packet. A MAX_DATAGRAM of 1400 bytes needs 1448. Today the 41-byte probes get through, so the path settles on
        ipv6, and every full-size datagram is then lost without a word. Fix: probe each way at full size before
        using it for data (and drop to the next way, here the relay, when only small probes answer), or keep each
        datagram within 1232 bytes on IPv6."""
        v4, _ = self.relay()
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL, relay_config=self.vps(v4), v6_mtu=1280)
        b = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL, relay_config=self.vps(v4))
        mesh(a, b)
        time.sleep(5)
        big = os.urandom(MAX_DATAGRAM)
        a.paths[b.id].send(big)
        self.assertTrue(wait_for(lambda: big in b.got, 5), (self.kinds(a, b), list(a.pc.v6_router.dropped)[-1:]))


class FriendTests(Base):
    def friend_config(self, identity):
        return {"friend": {"owner": OWNER.sign_public.hex(),
                           "pass": make_pass(OWNER.sign, identity.user_id, identity.sign_public).hex()}}

    def test_a_dual_stack_friend_bridges_an_ipv6_only_member_and_an_ipv4_only_one(self):
        a = Member(self, natsim.UDP_BLOCKED, natsim.V6_STATEFUL, relay_config=self.friend_config)
        b = Member(self, natsim.SYMMETRIC, natsim.NO_V6, relay_config=self.friend_config)
        friend = Member(self, natsim.FULL_CONE, natsim.V6_STATEFUL, relay_config=self.friend_config, friend_relay=True)
        mesh(a, b, friend)
        self.connects(a, b, "friend", seconds=25)


if __name__ == "__main__":
    unittest.main()
