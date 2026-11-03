"""The NAT simulator (natsim.py) itself, and the punch-through path of 3.0 put through it: which pairs of routers it gets
across, and which it cannot (the gap a relay closes)."""

import ipaddress
import socket
import threading
import time
import unittest

import natsim
from privatepartynet.dht import Dht
from privatepartynet.identity import Identity
from privatepartynet.punch import HostPunch, PunchError, TunnelClient, TunnelHost, join_through, learn_mapping
from test_dht import FakeNode
from test_punch import Base

CONES = (natsim.FULL_CONE, natsim.RESTRICTED, natsim.PORT_RESTRICTED)


def received(sock, timeout=0.5):
    """(data, source) of the next packet on `sock`, or None when nothing comes within `timeout` seconds."""
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(2048)
    except (socket.timeout, ConnectionResetError):
        return None


class SimTests(unittest.TestCase):
    def setUp(self):
        self.internet = natsim.Internet()
        self.addCleanup(self.internet.close)

    def udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        return sock

    def server(self, router=None):
        """A socket on the internet and its public address; servers on one `router` share its public address."""
        sock = self.udp()
        return sock, (router.forward(sock) if router else self.internet.server(sock))

    def behind(self, kind, **options):
        router = self.internet.router(kind, **options)
        sock = router.socket()
        self.addCleanup(sock.close)
        return router, sock

    def mapped(self, sock, server, public):
        """Send from `sock` to a server and return the public address the server saw it come from."""
        sock.sendto(b"hello", public)
        data, source = received(server)
        self.assertEqual(data, b"hello")
        return source


class KindTests(SimTests):
    def test_a_full_cone_lets_anyone_in_once_the_mapping_exists(self):
        router, pc = self.behind(natsim.FULL_CONE)
        first, first_public = self.server()
        stranger, stranger_public = self.server()
        mapping = self.mapped(pc, first, first_public)
        self.assertEqual(mapping[0], router.address)
        stranger.sendto(b"unasked", mapping)
        self.assertEqual(received(pc), (b"unasked", stranger_public), "seen from the stranger's public address")

    def test_a_restricted_cone_lets_in_any_port_of_an_address_it_sent_to(self):
        router, pc = self.behind(natsim.RESTRICTED)
        vps = self.internet.router(natsim.FULL_CONE)
        one, one_public = self.server(vps)
        other_port, other_port_public = self.server(vps)
        stranger, _ = self.server()
        mapping = self.mapped(pc, one, one_public)
        other_port.sendto(b"same address", mapping)
        self.assertEqual(received(pc), (b"same address", other_port_public))
        stranger.sendto(b"unasked", mapping)
        self.assertIsNone(received(pc))
        self.assertEqual(router.dropped[-1][0], "not an answer to anything sent from there")

    def test_a_port_restricted_cone_keeps_one_port_and_lets_in_only_where_it_sent(self):
        router, pc = self.behind(natsim.PORT_RESTRICTED)
        vps = self.internet.router(natsim.FULL_CONE)
        one, one_public = self.server(vps)
        other_port, _ = self.server(vps)
        elsewhere, elsewhere_public = self.server()
        mapping = self.mapped(pc, one, one_public)
        self.assertEqual(self.mapped(pc, elsewhere, elsewhere_public), mapping, "the same port for every destination")
        other_port.sendto(b"same address, other port", mapping)
        self.assertIsNone(received(pc))
        one.sendto(b"answer", mapping)
        self.assertEqual(received(pc), (b"answer", one_public))

    def test_a_symmetric_router_makes_a_new_port_for_every_destination(self):
        router, pc = self.behind(natsim.SYMMETRIC)
        one, one_public = self.server()
        two, two_public = self.server()
        to_one, to_two = self.mapped(pc, one, one_public), self.mapped(pc, two, two_public)
        self.assertEqual(to_one[0], to_two[0])
        self.assertNotEqual(to_one[1], to_two[1])
        self.assertEqual(router.public_address(pc, one_public), to_one)
        two.sendto(b"through the other mapping", to_one)
        self.assertIsNone(received(pc))
        one.sendto(b"answer", to_one)
        self.assertEqual(received(pc), (b"answer", one_public))

    def test_udp_blocked_lets_nothing_out_and_tcp_still_works(self):
        router, pc = self.behind(natsim.UDP_BLOCKED)
        server, public = self.server()
        pc.sendto(b"hello", public)
        self.assertIsNone(received(server))
        self.assertEqual(router.dropped[-1][0], "UDP is blocked")
        self.assertIsNone(router.public_address(pc))
        with self.assertRaises(ValueError):
            router.forward(pc)
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        socket.create_connection(listener.getsockname(), timeout=2).close()

    def test_stun_through_the_routers_tells_easy_from_hard_like_the_app_does(self):
        stun = self.internet.stun_servers(2)
        for kind in CONES + (natsim.SYMMETRIC,):
            with self.subTest(kind=kind):
                router, pc = self.behind(kind)
                address, port, nat = learn_mapping(pc, stun, timeout=0.5)
                self.assertEqual(address, router.address)
                self.assertEqual(nat, "hard" if kind == natsim.SYMMETRIC else "easy")
        _, blocked = self.behind(natsim.UDP_BLOCKED)
        with self.assertRaisesRegex(PunchError, "no STUN server answered"):
            learn_mapping(blocked, stun, timeout=0.3)

    def test_a_forwarded_port_lets_anyone_in_even_on_a_symmetric_router(self):
        router, pc = self.behind(natsim.SYMMETRIC)
        public = router.forward(pc)
        stranger, stranger_public = self.server()
        stranger.sendto(b"unasked", public)
        self.assertEqual(received(pc), (b"unasked", stranger_public))
        self.assertEqual(self.mapped(pc, stranger, stranger_public), public, "its own packets leave from it too")

    def test_a_mapping_left_unused_times_out(self):
        router, pc = self.behind(natsim.PORT_RESTRICTED, mapping_seconds=0.3)
        server, public = self.server()
        mapping = self.mapped(pc, server, public)
        time.sleep(0.5)
        server.sendto(b"too late", mapping)
        self.assertIsNone(received(pc))
        self.assertEqual(router.dropped[-1][0], "the mapping timed out")
        self.assertNotEqual(self.mapped(pc, server, public), mapping, "the next packet out gets a new port")

    def test_packets_to_ordinary_addresses_are_left_alone(self):
        _, pc = self.behind(natsim.SYMMETRIC)
        plain = self.udp()
        pc.sendto(b"hello", plain.getsockname())
        self.assertEqual(received(plain), (b"hello", pc.getsockname()))

    def test_a_socket_on_no_router_cannot_reach_the_internet_through_it(self):
        server, public = self.server()
        stray = self.udp()
        stray.sendto(b"hello", public)
        self.assertIsNone(received(server))

    def test_adopting_puts_sockets_that_other_code_binds_behind_the_router(self):
        router = self.internet.router(natsim.SYMMETRIC)
        server, public = self.server()
        with router.adopting():
            made = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(made.close)
            made.bind(("0.0.0.0", 0))
        elsewhere = []
        thread = threading.Thread(target=lambda: elsewhere.append(self.udp()))
        with router.adopting():
            thread.start()
            thread.join()
        self.assertEqual(self.mapped(made, server, public)[0], router.address)
        elsewhere[0].sendto(b"hello", public)
        self.assertIsNone(received(server), "a socket bound in another thread is not adopted unless any_thread=True")


class ProbeTests(SimTests):
    """Two PCs that learn their public addresses by STUN and send to each other at the same time, as the punch does."""

    def punch(self, kind_a, kind_b):
        stun = self.internet.stun_servers(2)
        (_, a), (_, b) = self.behind(kind_a), self.behind(kind_b)
        a_public, b_public = learn_mapping(a, stun, 0.5)[:2], learn_mapping(b, stun, 0.5)[:2]
        for _ in range(3):
            a.sendto(b"from a", b_public)
            b.sendto(b"from b", a_public)
        return received(a), received(b)

    def test_two_port_restricted_routers_open_for_each_other(self):
        at_a, at_b = self.punch(natsim.PORT_RESTRICTED, natsim.PORT_RESTRICTED)
        self.assertEqual(at_a[0], b"from b")
        self.assertEqual(at_b[0], b"from a")

    def test_two_symmetric_routers_never_let_the_probes_in(self):
        self.assertEqual(self.punch(natsim.SYMMETRIC, natsim.SYMMETRIC), (None, None))


def arrived(pc, timeout=0.5):
    """(data, source) of the next datagram for a natsim PC, or None when nothing comes within `timeout` seconds."""
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        got = pc.receive(0.05)
        if got is not None:
            return got
    return None


class V6Tests(SimTests):
    def pc(self, v6, v4=natsim.SYMMETRIC, **options):
        return self.internet.pc(v4=v4, v6=v6, **options)

    def test_an_ipv6_address_is_global_and_keeps_the_pcs_port(self):
        pc = self.pc(natsim.V6_STATEFUL)
        (kind, address), = pc.addresses
        self.assertEqual(kind, "ipv6")
        self.assertIn(ipaddress.ip_address(address), natsim.V6_NETWORK)
        other = self.pc(natsim.V6_OPEN)
        pc.sendto(b"hello", (other.address6, other.port))
        self.assertEqual(arrived(other), (b"hello", (address, pc.port)), "no NAT: the other side sees the PC itself")

    def test_a_stateful_firewall_lets_in_only_answers(self):
        pc, other = self.pc(natsim.V6_STATEFUL), self.pc(natsim.V6_OPEN)
        other.sendto(b"unasked", (pc.address6, pc.port))
        self.assertIsNone(arrived(pc))
        self.assertEqual(pc.v6_router.dropped[-1][0], "not an answer to anything sent from there")
        pc.sendto(b"hello", (other.address6, other.port))
        self.assertEqual(arrived(other)[0], b"hello")
        other.sendto(b"answer", (pc.address6, pc.port))
        self.assertEqual(arrived(pc), (b"answer", (other.address6, other.port)))

    def test_two_stateful_firewalls_open_for_each_other_when_both_send(self):
        a, b = self.pc(natsim.V6_STATEFUL), self.pc(natsim.V6_STATEFUL)
        for _ in range(3):
            a.sendto(b"from a", (b.address6, b.port))
            b.sendto(b"from b", (a.address6, a.port))
        self.assertEqual(arrived(a)[0], b"from b")
        self.assertEqual(arrived(b)[0], b"from a")

    def test_a_network_that_blocks_inbound_udp_lets_not_even_answers_in(self):
        pc, other = self.pc(natsim.V6_BLOCKS_INBOUND), self.pc(natsim.V6_OPEN)
        pc.sendto(b"hello", (other.address6, other.port))
        self.assertEqual(arrived(other)[0], b"hello", "it can still send")
        other.sendto(b"answer", (pc.address6, pc.port))
        self.assertIsNone(arrived(pc))
        self.assertEqual(pc.v6_router.dropped[-1][0], "inbound UDP is blocked")
        with self.assertRaises(ValueError):
            pc.v6_router.forward(pc.v6)

    def test_a_pc_with_no_ipv6_offers_none_and_cannot_send_there(self):
        pc, other = self.pc(natsim.NO_V6), self.pc(natsim.V6_OPEN)
        self.assertIsNone(pc.v6)
        self.assertEqual(pc.addresses, [])
        self.assertFalse(pc.sendto(b"hello", (other.address6, other.port)))
        with self.assertRaises(ValueError):
            self.internet.router(natsim.NO_V6)

    def test_ipv4_still_goes_through_the_pcs_router(self):
        pc = self.pc(natsim.V6_OPEN, v4=natsim.PORT_RESTRICTED)
        server, public = self.server()
        pc.sendto(b"hello", public)
        data, source = received(server)
        self.assertEqual(source[0], pc.v4_router.address)
        server.sendto(b"answer", source)
        self.assertEqual(arrived(pc), (b"answer", public))

    def test_a_dual_stack_server_answers_on_the_family_it_was_asked_on(self):
        server = self.udp()
        public, global_ = self.internet.server(server), self.internet.server(server, family=6)
        self.assertEqual(global_[1], server.getsockname()[1], "no NAT on IPv6: the server's own port")
        pc = self.pc(natsim.V6_STATEFUL, v4=natsim.PORT_RESTRICTED)
        pc.sendto(b"over v4", public)
        pc.sendto(b"over v6", global_)
        for _ in range(2):
            data, source = received(server)
            server.sendto(data + b" back", source)
        answers = {arrived(pc), arrived(pc)}
        self.assertEqual(answers, {(b"over v4 back", public), (b"over v6 back", global_)})

    def test_an_mtu_drops_what_would_not_fit(self):
        pc, other = self.pc(natsim.V6_OPEN, v6_mtu=1280), self.pc(natsim.V6_OPEN)
        pc.sendto(b"x" * 1232, (other.address6, other.port))
        self.assertEqual(len(arrived(other)[0]), 1232, "1232 bytes and 48 of header make 1280")
        pc.sendto(b"x" * 1233, (other.address6, other.port))
        self.assertIsNone(arrived(other))
        self.assertIn("MTU of 1280", pc.v6_router.dropped[-1][0])

    def test_an_address_nobody_has_loses_the_datagram_quietly(self):
        pc = self.pc(natsim.V6_OPEN)
        self.assertTrue(pc.sendto(b"hello", ("2001:db8:77::ffff", 9)))
        self.assertTrue(pc.sendto(b"hello", ("2001:0db8:0077:0:0:0:0:ffff", 9)), "any spelling of an address")


class TunnelThroughRoutersTests(Base):
    """The QUIC tunnel of punch.py, with the host probing the joiner's public address and the joiner dialling the
    host's, as join_through does once both know them. The joiner only ever aims at the host's address from STUN, so a
    symmetric host (which answers from a new port) is never reached, nor is a symmetric joiner by a port-restricted
    host. Only two symmetric routers are refused up front; the other failures wait out connect_seconds."""

    # (host's router, joiner's router) -> whether the tunnel opens today. A relay should turn every False into True.
    EXPECTED = {(host, joiner): not (host == natsim.SYMMETRIC or
                                     (host == natsim.PORT_RESTRICTED and joiner == natsim.SYMMETRIC))
                for host in CONES + (natsim.SYMMETRIC,) for joiner in CONES + (natsim.SYMMETRIC,)}

    def tunnel(self, host_kind, joiner_kind):
        internet = natsim.Internet()
        self.addCleanup(internet.close)
        stun = internet.stun_servers(2)
        host_router, joiner_router = internet.router(host_kind), internet.router(joiner_kind)
        host_sock, joiner_sock = host_router.socket(), joiner_router.socket()
        self.addCleanup(host_sock.close)
        self.addCleanup(joiner_sock.close)  # a TunnelClient that fails leaves its socket to the caller
        host_public, joiner_public = learn_mapping(host_sock, stun, 0.5), learn_mapping(joiner_sock, stun, 0.5)
        host = TunnelHost(host_sock, self.certfile, self.keyfile, self.party.port)
        self.addCleanup(host.close)
        host.probe(joiner_public[:2], seconds=4)
        client = TunnelClient(joiner_sock, host_public[:2], self.fingerprint, connect_seconds=3)
        self.addCleanup(client.close)
        return client

    def test_which_pairs_of_routers_the_tunnel_gets_through(self):
        for (host_kind, joiner_kind), opens in self.EXPECTED.items():
            with self.subTest(host=host_kind, joiner=joiner_kind):
                if opens:
                    status, _ = self.through(self.tunnel(host_kind, joiner_kind).port, "GET", "/ask?challenge=1")
                    self.assertEqual(status, 200)
                else:
                    with self.assertRaises(Exception):
                        self.tunnel(host_kind, joiner_kind)


class RendezvousThroughRoutersTests(Base):
    """The whole 3.0 path, HostPunch and join_through with a fake DHT, with each PC behind its own simulated router."""

    def setUp(self):
        super().setUp()
        from privatepartynet import dht as dht_module

        dht_module._known.clear()
        self.network = []
        for _ in range(12):
            self.network.append(FakeNode(self.network))
        for node in self.network:
            self.addCleanup(node.close)
        self.internet = natsim.Internet()
        self.addCleanup(self.internet.close)
        self.stun = self.internet.stun_servers(2)
        self.host_identity = Identity.create()
        self.secret = b"invite-secret-01"

    def dht(self):
        return Dht(bootstrap=[self.network[0].address], timeout=0.5, deadline=6.0)

    def join(self, host_kind, joiner_kind):
        host_router, joiner_router = self.internet.router(host_kind), self.internet.router(joiner_kind)
        with host_router.adopting():
            host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile,
                             self.party.port, lambda: [self.secret], dht_factory=self.dht, stun_servers=self.stun,
                             refresh=1.0, poll=0.5)
        self.addCleanup(host.close)
        self.assertEqual(host.mapping[0], host_router.address, "the host's socket is behind its router")
        time.sleep(2.0)  # the first record is published
        with joiner_router.adopting():
            client = join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret,
                                  self.fingerprint, dht_factory=self.dht, stun_servers=self.stun, connect_seconds=20)
        self.addCleanup(client.close)
        return host, client

    def test_two_port_restricted_routers_get_through(self):
        host, client = self.join(natsim.PORT_RESTRICTED, natsim.PORT_RESTRICTED)
        status, _ = self.through(client.port, "GET", "/ask?challenge=1")
        self.assertEqual(status, 200)
        self.assertTrue(host.seen)

    def test_two_symmetric_routers_are_refused_which_is_the_gap_a_relay_closes(self):
        with self.assertRaisesRegex(PunchError, "new port for every destination"):
            self.join(natsim.SYMMETRIC, natsim.SYMMETRIC)


if __name__ == "__main__":
    unittest.main()
