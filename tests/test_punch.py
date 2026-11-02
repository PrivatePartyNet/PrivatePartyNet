"""The punch-through path on this PC: QUIC tunnel, certificate pinning, DHT rendezvous (fake DHT and STUN), and the
party's handling of tunnelled joiners. A real router that lets nothing in cannot be made on one PC, so the punching
itself is proven only between two real networks (see tools\\spikes)."""

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from privatepartynet import punch
from privatepartynet.cert import ensure_certificate
from privatepartynet.connect import _connect, certificate_fingerprint
from privatepartynet.dht import Dht
from privatepartynet.identity import Identity
from privatepartynet.punch import (HostPunch, PunchError, TunnelClient, TunnelHost, join_through, mailbox_key, read_record,
                            record, tunnel_source)
from privatepartynet.server import Party, is_local_address
from test_connect import FakeStun
from test_dht import FakeNode


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.certfile, self.keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.fingerprint = certificate_fingerprint(Path(self.certfile).read_text(encoding="utf-8"))
        self.party = Party("river-stone-42", 0, self.certfile, self.keyfile, party_name="Heroes night", host_name="Host",
                           failed_login_delay=0, heartbeat=0.2, server_id="feedfacecafebeef")
        self.party.start()
        self.addCleanup(self.party.stop)

    def udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        return sock

    def through(self, port, method, path, body=None):
        connection = _connect("127.0.0.1", port, self.fingerprint, 5)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


class TunnelTests(Base):
    def tunnel(self, fingerprint=None):
        host_sock, guest_sock = self.udp(), self.udp()
        host = TunnelHost(host_sock, self.certfile, self.keyfile, self.party.port)
        self.addCleanup(host.close)
        client = TunnelClient(guest_sock, host_sock.getsockname(), fingerprint or self.fingerprint, connect_seconds=8)
        self.addCleanup(client.close)
        return host, client

    def test_the_party_answers_through_the_tunnel_with_the_same_certificate(self):
        _, client = self.tunnel()
        status, body = self.through(client.port, "GET", "/ask?challenge=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["server"], "feedfacecafebeef")

    def test_several_connections_share_one_tunnel(self):
        _, client = self.tunnel()
        results = []

        def ask():
            results.append(self.through(client.port, "GET", "/ask?ticket=none")[0])

        threads = [threading.Thread(target=ask) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(results, [200] * 6)

    def test_a_joiner_through_the_tunnel_is_outside_the_network_and_needs_the_password(self):
        _, client = self.tunnel()
        status, body = self.through(client.port, "POST", "/ask", {"name": "Far", "password": ""})
        self.assertEqual(status, 403, "no password, from outside")
        self.assertIn(b"password", body)
        status, _ = self.through(client.port, "POST", "/ask", {"name": "Far", "password": "river-stone-42"})
        self.assertEqual(status, 200)
        (waiting,) = self.party.pending_requests()
        self.assertTrue(waiting["address"].startswith("127.1."))
        self.assertFalse(is_local_address(waiting["address"]))

    def test_a_wrong_host_certificate_ends_the_tunnel_before_any_request(self):
        host_sock, guest_sock = self.udp(), self.udp()
        host = TunnelHost(host_sock, self.certfile, self.keyfile, self.party.port)
        self.addCleanup(host.close)
        with self.assertRaises(PunchError):
            TunnelClient(guest_sock, host_sock.getsockname(), "00" * 32, connect_seconds=8)

    def test_nobody_answering_gives_up_in_time(self):
        guest_sock, silent = self.udp(), self.udp()
        self.addCleanup(silent.close)
        started = time.monotonic()
        with self.assertRaises(Exception):
            TunnelClient(guest_sock, silent.getsockname(), self.fingerprint, connect_seconds=2)
        self.assertLess(time.monotonic() - started, 10)

    def test_each_joiner_gets_their_own_source_address(self):
        self.assertNotEqual(tunnel_source(("203.0.113.5", 1)), tunnel_source(("203.0.113.5", 2)))
        self.assertTrue(tunnel_source(("198.51.100.7", 40000)).startswith("127.1."))


class RecordTests(unittest.TestCase):
    def test_records_round_trip_and_go_stale(self):
        from privatepartynet.dht import bdecode, bencode

        value = bdecode(bencode(record("203.0.113.5", 40000, "easy")))
        self.assertEqual(read_record(value), ("203.0.113.5", 40000, "easy"))
        old = dict(value)
        old[b"t"] = int(time.time()) - 3600
        self.assertIsNone(read_record(old))
        for bad in ({}, {b"a": b"nonsense", b"p": 1, b"t": int(time.time())}, {b"a": b"1.2.3.4", b"p": 0, b"t": int(time.time())}, "x"):
            self.assertIsNone(read_record(bad))

    def test_the_mailbox_key_comes_from_the_invite_secret(self):
        one = mailbox_key(b"s" * 16).public_key().public_bytes_raw()
        self.assertEqual(one, mailbox_key(b"s" * 16).public_key().public_bytes_raw())
        self.assertNotEqual(one, mailbox_key(b"t" * 16).public_key().public_bytes_raw())


class RendezvousTests(Base):
    """The whole path with a fake DHT on this PC and STUN servers that report each socket's own address (no NAT)."""

    def setUp(self):
        super().setUp()
        from privatepartynet import dht as dht_module

        dht_module._known.clear()
        self.network = []
        for _ in range(12):
            self.network.append(FakeNode(self.network))
        for node in self.network:
            self.addCleanup(node.close)
        self.stun = [FakeStun(), FakeStun()]
        for fake in self.stun:
            self.addCleanup(fake.close)
        self.host_identity = Identity.create()
        self.secret = b"invite-secret-01"

    def dht(self):
        return Dht(bootstrap=[self.network[0].address], timeout=0.5, deadline=6.0)

    def test_a_joiner_finds_the_host_punches_and_reaches_the_party(self):
        stun = tuple(fake.address for fake in self.stun)
        host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile, self.party.port,
                         lambda: [self.secret], dht_factory=self.dht, stun_servers=stun, refresh=1.0, poll=0.5)
        self.addCleanup(host.close)
        time.sleep(2.0)  # the first record is published
        client = join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                              dht_factory=self.dht, stun_servers=stun, connect_seconds=20)
        self.addCleanup(client.close)
        status, body = self.through(client.port, "GET", "/ask?challenge=1")
        self.assertEqual(status, 200)
        self.assertTrue(host.seen, "the host saw the joiner's mailbox and opened the way for them")

    def test_no_host_record_says_so(self):
        stun = tuple(fake.address for fake in self.stun)
        with self.assertRaisesRegex(PunchError, "not found"):
            join_through(Identity.create().sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                         dht_factory=self.dht, stun_servers=stun, connect_seconds=5, find_seconds=3)

    def test_two_hard_routers_are_refused_plainly(self):
        ports = iter(range(50000, 50100))
        hard = [FakeStun(lambda source: ("203.0.113.9", next(ports))) for _ in range(2)]
        for fake in hard:
            self.addCleanup(fake.close)
        stun = tuple(fake.address for fake in hard)
        dht = self.dht()
        dht.put_mutable(self.host_identity._sign, record("203.0.113.5", 40000, "hard"), int(time.time()),
                        punch.host_salt("feedfacecafebeef"))
        dht.close()
        with self.assertRaisesRegex(PunchError, "new port for every destination"):
            join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                         dht_factory=self.dht, stun_servers=stun, connect_seconds=5)

    def test_a_host_record_signed_by_someone_else_is_not_trusted(self):
        stun = tuple(fake.address for fake in self.stun)
        impostor = Identity.create()
        dht = self.dht()
        dht.put_mutable(impostor._sign, record("203.0.113.66", 40000, "easy"), int(time.time()),
                        punch.host_salt("feedfacecafebeef"))
        dht.close()
        with self.assertRaisesRegex(PunchError, "not found"):
            join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                         dht_factory=self.dht, stun_servers=stun, connect_seconds=5, find_seconds=3)


if __name__ == "__main__":
    unittest.main()


class JoinerPunchTests(RendezvousTests):
    """The Joiner falls back to the punch-through when no address in the invite answers."""

    def test_an_invite_whose_addresses_are_dead_still_gets_in_through_the_routers(self):
        from unittest import mock
        from privatepartynet import connect

        stun = tuple(fake.address for fake in self.stun)
        host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile, self.party.port,
                         lambda: [self.secret], dht_factory=self.dht, stun_servers=stun, refresh=1.0, poll=0.5)
        self.addCleanup(host.close)
        time.sleep(2.0)
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        invite = connect.decode_invite(connect.encode_invite(
            "Heroes night", "river-stone-42", [["public", "127.0.0.1", dead_port]], self.fingerprint,
            server_id="feedfacecafebeef", host_sign=self.host_identity.sign_public.hex(), secret=self.secret.hex()))
        self.assertEqual(invite["secret"], self.secret.hex())
        original = punch.join_through
        with mock.patch.object(punch, "join_through", lambda *a, **k: original(*a, dht_factory=self.dht, stun_servers=stun,
                                                                              connect_seconds=20)):
            joiner = connect.Joiner(invite, "Far Away", identity=Identity.create(), my_lan=[], poll=0.1, timeout=3.0)
            thread = threading.Thread(target=joiner.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and joiner.status["state"] != "waiting":
                time.sleep(0.1)
        self.assertEqual(joiner.status["state"], "waiting", joiner.status)
        (request,) = self.party.pending_requests()
        self.assertEqual(request["name"], "Far Away")
        self.assertTrue(request["address"].startswith("127.1."), "the party sees a joiner from outside")
        self.party.decide_request(self.party.host_id, request["id"], True)
        thread.join(10)
        self.assertEqual(joiner.status["state"], "allowed")
        self.assertTrue(joiner.status["url"].startswith("https://127.0.0.1:"))
        self.assertIsNotNone(joiner.tunnel, "the tunnel stays open for the party window")
        joiner.tunnel.close()

    def test_an_old_invite_without_the_secret_says_why_and_does_not_try(self):
        from privatepartynet import connect

        invite = connect.decode_invite(connect.encode_invite("P", "x", [["public", "127.0.0.1", 9]], self.fingerprint))
        joiner = connect.Joiner(invite, "Far", my_lan=[], timeout=1.0)
        joiner.run()
        self.assertEqual(joiner.status["state"], "failed")
        self.assertNotIn("through the routers", joiner.status["message"])
