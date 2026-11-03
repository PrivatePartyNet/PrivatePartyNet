"""Every pair of home routers, through natsim, with the group's relay server there: the paths between members and the
join tunnel both connect in all sixteen cases, directly where the routers allow it and through the relay where they
do not. A member whose network lets no UDP out reaches the relay over TLS."""

import itertools
import sys
import time
from pathlib import Path

import natsim
from privatepartynet import punch
from privatepartynet import relayproto as rp
from privatepartynet.dht import Dht
from privatepartynet.identity import Identity
from privatepartynet.pathway import PathManager, UdpTransport
from privatepartynet.punch import HostPunch, join_through
from test_dht import FakeNode
from test_punch import Base

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
from ppn_relay import ThreadedRelay, certificate_pin  # noqa: E402

SECRET = "the group's relay secret"
KINDS = (natsim.FULL_CONE, natsim.RESTRICTED, natsim.PORT_RESTRICTED, natsim.SYMMETRIC)
# Where direct paths cannot work: one symmetric router facing another, or facing a port-restricted one.
NEEDS_RELAY = {(natsim.SYMMETRIC, natsim.SYMMETRIC), (natsim.SYMMETRIC, natsim.PORT_RESTRICTED),
               (natsim.PORT_RESTRICTED, natsim.SYMMETRIC)}


def wait_for(condition, seconds):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class PathMatrixTests(Base):
    def relay(self, internet):
        relay = ThreadedRelay([SECRET], host="0.0.0.0", udp_port=0, tls_port=0, certfile=self.certfile,
                              keyfile=self.keyfile)
        self.addCleanup(relay.close)
        return relay, internet.server(relay.udp_port)

    def members(self, internet, kinds, relay, public):
        stun = list(internet.stun_servers(2))
        found = []
        for kind in kinds:
            identity, transport = Identity.create(), UdpTransport()
            internet.router(kind).attach(transport.v4)
            config = {"vps": [{"host": public[0], "port": public[1], "tls_port": relay.tls_port,
                               "pin": certificate_pin(self.certfile),
                               "credential": rp.member_credential(SECRET, identity.user_id).hex()}]}
            manager = PathManager(identity, {}, config, transport=transport, addresses=[], stun_servers=stun,
                                  probe_network=False)
            self.addCleanup(manager.stop)
            found.append((identity, manager))
        return found

    def connect(self, kind_a, kind_b, seconds=15, best=None):
        internet = natsim.Internet()
        self.addCleanup(internet.close)
        relay, public = self.relay(internet)
        (ia, a), (ib, b) = self.members(internet, (kind_a, kind_b), relay, public)
        got_a, got_b = [], []
        a.set_on_candidates(lambda c: b.path_to(ia.user_id, c))
        b.set_on_candidates(lambda c: a.path_to(ib.user_id, c))
        pa, pb = a.path_to(ib.user_id, b.local_candidates()), b.path_to(ia.user_id, a.local_candidates())
        pa.set_receiver(got_a.append)
        pb.set_receiver(got_b.append)
        wanted = (lambda: pa.kind == best and pb.kind == best) if best else (lambda: pa.kind and pb.kind)
        self.assertTrue(wait_for(wanted, seconds), (kind_a, kind_b, a.state(), b.state()))

        def both_ways():
            pa.send(b"from a")
            pb.send(b"from b")
            return b"from a" in got_b and b"from b" in got_a

        self.assertTrue(wait_for(both_ways, 5))
        return pa.kind, pb.kind, a, b

    def test_every_pair_of_routers_connects(self):
        for kind_a, kind_b in itertools.product(KINDS, KINDS):
            with self.subTest(a=kind_a, b=kind_b):
                # Whichever answers first carries the pair; a direct way, where there is one, takes over when it opens.
                best = "vps" if (kind_a, kind_b) in NEEDS_RELAY else "punch"
                path_a, path_b, _, _ = self.connect(kind_a, kind_b, best=best)
                self.assertEqual((path_a, path_b), (best, best))

    def test_a_member_whose_network_lets_no_udp_out_reaches_the_relay_over_tls(self):
        path_a, path_b, a, _ = self.connect(natsim.UDP_BLOCKED, natsim.PORT_RESTRICTED, seconds=25)
        self.assertEqual((path_a, path_b), ("vps", "vps"))
        self.assertEqual([c.transport for c in a._relays.values()], ["tls"])


class JoinMatrixTests(Base):
    """punch.py's join: the host publishes on the DHT and waits on the relay; the joiner punches where it can and goes
    through the relay where it cannot, without waiting out the whole punching time."""

    def setUp(self):
        super().setUp()
        from privatepartynet import dht as dht_module

        dht_module._known.clear()
        self.network = []
        for _ in range(12):
            self.network.append(FakeNode(self.network))
        for node in self.network:
            self.addCleanup(node.close)
        self.secret = b"invite-secret-03"

    def dht(self):
        return Dht(bootstrap=[self.network[0].address], timeout=0.5, deadline=6.0)

    def join(self, host_kind, joiner_kind):
        internet = natsim.Internet()
        self.addCleanup(internet.close)
        stun = internet.stun_servers(2)
        relay = ThreadedRelay([SECRET], host="0.0.0.0", udp_port=0)
        self.addCleanup(relay.close)
        public = internet.server(relay.udp_port)
        self.party.set_relay(self.party.host_id, {"host": public[0], "port": public[1], "tls_port": 0,
                                                  "secret": SECRET})
        host_key = Identity.create()
        host_router, joiner_router = internet.router(host_kind), internet.router(joiner_kind)
        with host_router.adopting():
            host = HostPunch(host_key._sign, "feedfacecafebeef", self.certfile, self.keyfile, self.party.port,
                             lambda: [self.secret], dht_factory=self.dht, stun_servers=stun, refresh=1.0, poll=0.5,
                             relays=self.party.host_relays)
        self.addCleanup(host.close)
        time.sleep(2.0)  # the first host record is published
        from privatepartynet.connect import _read_relays

        relays = _read_relays(self.party.invite_relays(self.secret))
        started = time.monotonic()
        with joiner_router.adopting():
            client = join_through(host_key.sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                                  dht_factory=self.dht, stun_servers=stun, connect_seconds=20, find_seconds=10,
                                  relays=relays)
        self.addCleanup(client.close)
        return client, time.monotonic() - started

    def test_every_pair_of_routers_joins(self):
        for host_kind, joiner_kind in itertools.product(KINDS, KINDS):
            with self.subTest(host=host_kind, joiner=joiner_kind):
                client, seconds = self.join(host_kind, joiner_kind)
                status, _ = self.through(client.port, "GET", "/ask?challenge=1")
                self.assertEqual(status, 200)
                # Behind a hard router it does not wait out the 20 seconds of punching before taking the relay.
                self.assertLess(seconds, 20 if client.kind == "punch" else punch.QUICK_SECONDS + 15,
                                (host_kind, joiner_kind, client.kind))
                if host_kind == natsim.SYMMETRIC:
                    self.assertEqual(client.kind, "relay")
