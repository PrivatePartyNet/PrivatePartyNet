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
from privatepartynet import relayproto as rp
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

    def test_the_tunnel_takes_the_certificates_key_pin_too(self):
        from privatepartynet.cert import pins_of_pem

        spki = pins_of_pem(Path(self.certfile).read_text(encoding="utf-8"))[1]
        _, client = self.tunnel(fingerprint=("00" * 32, spki))  # an old whole-certificate pin, and the key's
        status, _ = self.through(client.port, "GET", "/ask?challenge=1")
        self.assertEqual(status, 200)

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

    def test_connections_opened_at_the_same_moment_each_get_a_stream_of_their_own(self):
        """Two connections that opened before either sent anything used to share one QUIC stream, and their TLS broke."""
        _, client = self.tunnel()
        start = threading.Barrier(16)
        results = []

        def ask():
            start.wait(5)
            try:
                results.append(self.through(client.port, "GET", "/ask?ticket=none")[0])
            except Exception as exc:  # a broken TLS connection is the failure this test is about
                results.append(repr(exc))

        threads = [threading.Thread(target=ask) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(results, [200] * 16)

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


class DhtBase(Base):
    """A fake DHT on this PC and STUN servers that report each socket's own address (no NAT)."""

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


class RendezvousTests(DhtBase):
    """The whole path with a fake DHT on this PC and STUN servers that report each socket's own address (no NAT)."""

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
        # with no NAT the joiner can get in before the host's next read of the mailbox, which is a DHT lookup away
        ends = time.monotonic() + 10
        while not host.seen and time.monotonic() < ends:
            time.sleep(0.1)
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
        with self.assertRaisesRegex(PunchError, "new port for every connection"):
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


@unittest.skipUnless(socket.has_ipv6, "no IPv6 on this PC")
class Ipv6JoinTests(DhtBase):
    """The join over IPv6, first where both PCs have it. On one PC that is the loopback address ::1, with no firewall
    in between; the firewall itself opens the same way as an IPv4 router, by both sending (see test_natsim)."""

    def host(self, stun=None, ipv6=("::1",)):
        stun = stun or tuple(fake.address for fake in self.stun)
        host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile, self.party.port,
                         lambda: [self.secret], dht_factory=self.dht, stun_servers=stun, refresh=1.0, poll=0.5,
                         ipv6=lambda: list(ipv6))
        self.addCleanup(host.close)
        time.sleep(2.0)  # the first record is published
        return host

    def join(self, ipv6=("::1",)):
        client = join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret, self.fingerprint,
                              dht_factory=self.dht, stun_servers=tuple(fake.address for fake in self.stun),
                              connect_seconds=20, ipv6=(lambda: list(ipv6)) if ipv6 else None)
        self.addCleanup(client.close)
        return client

    def test_records_carry_an_ipv6_address_beside_the_ipv4_one(self):
        from privatepartynet.dht import bdecode, bencode

        both = bdecode(bencode(record("203.0.113.5", 40000, "hard", v6=("2001:db8::5", 40001))))
        self.assertEqual(read_record(both), ("203.0.113.5", 40000, "hard"))
        self.assertEqual(punch.read_record6(both), ("2001:db8::5", 40001))
        only6 = bdecode(bencode(record(None, 0, "", v6=("2001:db8::5", 40001))))
        self.assertIsNone(read_record(only6))
        self.assertEqual(punch.read_record6(only6), ("2001:db8::5", 40001))
        self.assertIsNone(punch.read_record6(bdecode(bencode(record("203.0.113.5", 40000, "easy")))))

    def test_a_joiner_with_ipv6_reaches_the_host_over_it_first(self):
        host = self.host()
        self.assertEqual(host.mapping6[0], "::1")
        client = self.join()
        self.assertEqual(client.kind, "ipv6")
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)
        ends = time.monotonic() + 20  # the host reads the mailbox a lookup later; on one PC the joiner is in already
        while time.monotonic() < ends and not any(":" in address for address, _ in host.seen):
            time.sleep(0.2)
        self.assertTrue(any(":" in address for address, _ in host.seen), "the host opened its IPv6 side for the joiner")

    def test_a_host_whose_ipv4_lets_nothing_through_is_reached_over_ipv6(self):
        silent = FakeStun(silent=True)
        self.addCleanup(silent.close)
        host = self.host(stun=(silent.address,))
        self.assertIsNone(host.mapping, "no IPv4 address at all")
        client = self.join()
        self.assertEqual((client.kind, self.through(client.port, "GET", "/ask?challenge=1")[0]), ("ipv6", 200))

    def test_a_joiner_without_ipv6_goes_over_ipv4(self):
        self.host()
        client = self.join(ipv6=None)
        self.assertEqual(client.kind, "punch")
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)


class NoDht:
    """A DHT that finds nothing, as when the host's record is missing or the DHT cannot be reached."""

    def put_mutable(self, *args, **kwargs):
        pass

    def get_mutable(self, *args, **kwargs):
        return None

    def close(self):
        pass


class RelayJoinTests(Base):
    """The join tunnel through a relay: the group's relay server or a member who relays for others."""

    SECRET = "the group's relay secret"

    def setUp(self):
        super().setUp()
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
        from ppn_relay import ThreadedRelay

        self.relay = ThreadedRelay([self.SECRET], host="127.0.0.1", udp_port=0)
        self.addCleanup(self.relay.close)
        self.party.set_relay(self.party.host_id, {"host": "127.0.0.1", "port": self.relay.udp_port, "tls_port": 0,
                                                  "secret": self.SECRET})
        self.invite_secret = b"invite-secret-02"
        silent = FakeStun(silent=True)
        self.addCleanup(silent.close)
        self.host = HostPunch(Identity.create()._sign, "feedfacecafebeef", self.certfile, self.keyfile, self.party.port,
                              lambda: [self.invite_secret], dht_factory=NoDht, stun_servers=(silent.address,),
                              poll=0.5, relays=self.party.host_relays)
        self.addCleanup(self.host.close)

    def invite(self, **options):
        from privatepartynet import connect

        code = connect.encode_invite("Heroes night", "river-stone-42", [], self.fingerprint, secret=self.invite_secret.hex(),
                                     relays=self.party.invite_relays(self.invite_secret), **options)
        return connect.decode_invite(code)

    def test_a_host_that_nobody_can_punch_to_is_still_reached_through_the_relay_server(self):
        self.assertIsNone(self.host.mapping, "no STUN answer: the host waits on the relays only")
        (relay,) = self.invite()["relays"]
        self.assertEqual(relay["kind"], "vps")
        client = punch.join_relayed(self.invite_secret, self.fingerprint, [relay], connect_seconds=10)
        self.addCleanup(client.close)
        self.assertEqual(client.kind, "relay")
        status, body = self.through(client.port, "GET", "/ask?challenge=1")
        self.assertEqual(status, 200)
        status, _ = self.through(client.port, "POST", "/ask", {"name": "Far", "password": "river-stone-42"})
        self.assertEqual(status, 200)
        (waiting,) = self.party.pending_requests()
        self.assertFalse(is_local_address(waiting["address"]), "a joiner through a relay is from outside")
        self.assertGreater(self.relay.server.core.stats["forwarded"], 0)

    def test_join_through_falls_back_to_the_relay_when_the_host_is_not_found(self):
        invite = self.invite()
        client = join_through(Identity.create().sign_public.hex(), "feedfacecafebeef", self.invite_secret,
                              self.fingerprint, dht_factory=NoDht, find_seconds=0.5, relays=invite["relays"],
                              connect_seconds=10)
        self.addCleanup(client.close)
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)

    def test_a_wrong_host_certificate_through_the_relay_is_refused(self):
        with self.assertRaisesRegex(PunchError, "certificate differs"):
            punch.join_relayed(self.invite_secret, "00" * 32, self.invite()["relays"], connect_seconds=8)

    def test_a_relay_that_refuses_the_token_is_reported(self):
        (relay,) = self.invite()["relays"]
        forged = dict(relay, token=rp.member_credential("a guess", "00" * 16))
        with self.assertRaisesRegex(PunchError, "did not accept"):
            punch.join_relayed(self.invite_secret, self.fingerprint, [forged], connect_seconds=5)

    def test_an_invite_for_another_secret_meets_nobody(self):
        with self.assertRaises(PunchError):
            punch.join_relayed(b"invite-secret-99", self.fingerprint, self.invite()["relays"], connect_seconds=3)

    def test_a_member_who_relays_for_others_carries_the_join(self):
        from privatepartynet.pathway import PathManager

        self.party.owner = Identity.create()  # friend relays take passes the owner signs
        friend = PathManager(Identity.create(), {"friend_relay": True}, self.party.path_relay_for("22" * 16)["relay"],
                             addresses=[("upnp", "127.0.0.1")], probe_network=False)
        self.addCleanup(friend.stop)
        self.party.set_relay(self.party.host_id, None)
        self.party.friend_relays = lambda: [("127.0.0.1", friend.port)]
        self.host._sync_relays()
        (relay,) = self.invite()["relays"]
        self.assertEqual(relay["kind"], "friend")
        client = punch.join_relayed(self.invite_secret, self.fingerprint, [relay], connect_seconds=10)
        self.addCleanup(client.close)
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)
        self.assertGreater(friend.state()["relayed"]["forwarded"], 0)

    def test_the_joiner_uses_the_relay_when_no_address_answers(self):
        from privatepartynet import connect

        invite = self.invite()
        invite["candidates"] = [["public", "127.0.0.1", 9]]
        joiner = connect.Joiner(invite, "Far Away", identity=Identity.create(), my_lan=[], poll=0.1, timeout=2.0)
        thread = threading.Thread(target=joiner.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and joiner.status["state"] != "waiting":
            time.sleep(0.1)
        self.assertEqual(joiner.status["state"], "waiting", joiner.status)
        (request,) = self.party.pending_requests()
        self.party.decide_request(self.party.host_id, request["id"], True)
        thread.join(10)
        self.assertEqual(joiner.status["state"], "allowed")
        self.assertEqual(joiner.tunnel.kind, "relay")
        joiner.tunnel.close()


class InviteRelayTests(unittest.TestCase):
    def test_relays_round_trip_in_an_invite_and_bad_ones_are_dropped(self):
        from privatepartynet import connect

        token = rp.member_credential("s" * 16, "00" * 16).hex()
        friend = (bytes(rp.PASS_BYTES)).hex()
        relays = [["vps", "relay.example.net", 3479, 443, "ab" * 32, token], ["friend", "198.51.100.7", 40001, 0, "", friend],
                  ["friend", "a.name.instead", 1, 0, "", friend], ["vps", "x", 0, 0, "", token], ["other", "x", 1, 0, "", token],
                  ["vps", "x", 1, 0, "", "beef"], ["friend", "198.51.100.8", 1, 0, "", token]]
        invite = connect.decode_invite(connect.encode_invite("P", "pw", [], "cd" * 32, secret="ee" * 16, relays=relays))
        self.assertEqual([(r["kind"], r["host"], r["port"]) for r in invite["relays"]],
                         [("vps", "relay.example.net", 3479), ("friend", "198.51.100.7", 40001)])
        self.assertEqual(invite["relays"][0]["token"], bytes.fromhex(token))
        self.assertEqual(connect.decode_invite(connect.encode_invite("P", "pw", [], "cd" * 32))["relays"], [])
