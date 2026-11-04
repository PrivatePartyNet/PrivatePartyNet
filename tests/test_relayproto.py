"""The relay: credentials, the forwarding rules and limits, the server over UDP and over TLS, and the PC's side of it."""

import asyncio
import base64
import hashlib
import hmac
import socket
import ssl
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet import relayproto as rp
from privatepartynet.cert import ensure_certificate
from privatepartynet.identity import Identity
from privatepartynet.pathway import PassCredential, PassVerifier, make_pass
from privatepartynet.relayclient import RelayBridge

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
from ppn_relay import ThreadedRelay, certificate_pin, main as relay_main  # noqa: E402

ALICE, BOB, CAROL = "a1" * 16, "b2" * 16, "c3" * 16
CHANNEL = rp.channel_for("test", b"a pair key")


def wait_for(condition, seconds=5.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.verifier = rp.HmacVerifier(["secret"])

    def check(self, message, now=None):
        kind, channel, body = rp.parse(message)
        return self.verifier.check(channel, body, now if now is not None else time.time())

    def test_a_bind_names_its_user_until_the_credential_expires(self):
        credential = rp.member_credential("secret", ALICE, lifetime=600, now=1000)
        self.assertEqual(len(credential), rp.CREDENTIAL_BYTES)
        self.assertEqual(self.check(rp.bind(CHANNEL, credential, now=1030), now=1030)[0], ALICE)
        self.assertIsNone(self.check(rp.bind(CHANNEL, credential, now=1601), now=1601), "expired")

    def test_a_bind_is_fresh_for_a_minute_only(self):
        credential = rp.member_credential("secret", ALICE, now=1000)
        message = rp.bind(CHANNEL, credential, now=1000)
        self.assertIsNotNone(self.check(message, now=1050))
        self.assertIsNone(self.check(message, now=1000 + rp.FRESH_SECONDS + 1))

    def test_a_captured_bind_is_no_use_for_another_channel_or_with_its_user_changed(self):
        message = rp.bind(CHANNEL, rp.member_credential("secret", ALICE))
        other = bytes([rp.BIND]) + rp.channel_for("test", b"elsewhere") + message[rp.HEADER:]
        self.assertIsNone(self.check(other))
        changed = message[:rp.HEADER + 4] + bytes.fromhex(BOB) + message[rp.HEADER + 20:]
        self.assertIsNone(self.check(changed))
        with_cookie = message + bytes(16)  # a cookie added on the way is not covered by the proof
        self.assertIsNone(self.check(with_cookie))

    def test_the_key_never_travels_in_a_bind(self):
        credential = rp.member_credential("secret", ALICE)
        self.assertNotIn(credential[20:], rp.bind(CHANNEL, credential))
        self.assertNotIn(credential[20:36], rp.bind(CHANNEL, credential))

    def test_a_credential_from_another_secret_is_refused(self):
        self.assertIsNone(self.check(rp.bind(CHANNEL, rp.member_credential("other", ALICE))))

    def test_turn_credentials_are_coturns_rest_form(self):
        username, credential = rp.turn_credentials("secret", ALICE, lifetime=3600, now=1000)
        self.assertEqual(username, f"4600:{ALICE}")
        expected = base64.b64encode(hmac.new(b"secret", username.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(credential, expected)

    def test_channels_differ_by_label_and_secret(self):
        self.assertNotEqual(rp.channel_for("path", b"k1"), rp.channel_for("path", b"k2"))
        self.assertNotEqual(rp.channel_for("path", b"k1"), rp.channel_for("join", b"k1"))
        self.assertEqual(len(rp.channel_for("path", b"k1")), rp.CHANNEL_BYTES)

    def test_a_new_secret_is_long_and_plain(self):
        secret = rp.new_secret()
        self.assertEqual(len(secret), 32)
        self.assertTrue(secret.isalnum())


class PassTests(unittest.TestCase):
    """Friend relays: passes the owner signs, used with the member's own key."""

    def setUp(self):
        self.owner, self.member = Identity.create(), Identity.create()
        self.verifier = PassVerifier(self.owner.sign_public)

    def check(self, message):
        _, channel, body = rp.parse(message)
        return self.verifier.check(channel, body, time.time())

    def test_a_copied_bind_sent_again_and_again_costs_no_more_signatures(self):
        """A friend relay is someone's gaming PC: a flood of one captured BIND_PASS must not burn its CPU."""
        credential = PassCredential(make_pass(self.owner.sign, self.member.user_id, self.member.sign_public),
                                    self.member.sign)
        message = credential.bind(CHANNEL)
        for _ in range(200):
            self.assertEqual(self.check(message)[0], self.member.user_id)
        self.assertEqual(self.verifier.signatures, 2, "the owner's and the member's, once each")
        self.check(credential.bind(CHANNEL, now=time.time() + 1))  # the next BIND of the same member
        self.assertEqual(self.verifier.signatures, 3, "the pass is remembered: only the member's signature again")

    def test_forged_signatures_on_a_captured_pass_cost_one_check_each_and_are_refused(self):
        credential = PassCredential(make_pass(self.owner.sign, self.member.user_id, self.member.sign_public),
                                    Identity.create().sign)  # someone else's key: every signature is wrong
        for second in range(5):
            self.assertIsNone(self.check(credential.bind(CHANNEL, now=time.time() + second)))
        self.assertEqual(self.verifier.signatures, 6, "the owner's signature once, then only the member's")

    def test_a_pass_from_the_owner_used_with_the_members_key_is_accepted(self):
        credential = PassCredential(make_pass(self.owner.sign, self.member.user_id, self.member.sign_public),
                                    self.member.sign)
        self.assertEqual(self.check(credential.bind(CHANNEL))[0], self.member.user_id)

    def test_a_member_cannot_make_passes(self):
        forged = make_pass(self.member.sign, "dd" * 16, self.member.sign_public)  # signed by a member, not the owner
        self.assertIsNone(self.check(PassCredential(forged, self.member.sign).bind(CHANNEL)))

    def test_a_captured_pass_is_no_use_without_the_members_key(self):
        stolen = make_pass(self.owner.sign, self.member.user_id, self.member.sign_public)
        thief = Identity.create()
        self.assertIsNone(self.check(PassCredential(stolen, thief.sign).bind(CHANNEL)))

    def test_a_pass_bind_is_bound_to_its_channel_and_expires(self):
        credential = PassCredential(make_pass(self.owner.sign, self.member.user_id, self.member.sign_public, lifetime=-1),
                                    self.member.sign)
        self.assertIsNone(self.check(credential.bind(CHANNEL)), "expired")
        fresh = PassCredential(make_pass(self.owner.sign, self.member.user_id, self.member.sign_public), self.member.sign)
        message = fresh.bind(CHANNEL)
        moved = bytes([rp.BIND_PASS]) + rp.channel_for("test", b"elsewhere") + message[rp.HEADER:]
        self.assertIsNone(self.check(moved))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.core = rp.RelayCore([rp.HmacVerifier(["secret"])], rate=10_000, burst=4_000, user_rate=None,
                                 clock=lambda: self.now[0])

    def bind(self, user, end, channel=CHANNEL, secret="secret", core=None):
        """Bind as a real client does: answer the relay's address check with its cookie. Returns the final status."""
        core = core or self.core
        credential = rp.member_credential(secret, user)
        out = core.handle(rp.bind(channel, credential), end)
        status = out[0][1][rp.HEADER]
        if status == rp.CHECK:
            cookie = out[0][1][rp.HEADER + 1:]
            out = core.handle(rp.bind(channel, credential, cookie), end)
            status = out[0][1][rp.HEADER]
        self.assertEqual(out[0][0], end)
        return status

    def test_two_ends_of_a_channel_reach_each_other(self):
        self.assertEqual(self.bind(ALICE, "a"), rp.OK)
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"early"), "a"), [])  # nobody at the other end yet
        self.assertEqual(self.bind(BOB, "b"), rp.OK)
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"hello"), "a"), [("b", rp.deliver(CHANNEL, b"hello"))])
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"back"), "b"), [("a", rp.deliver(CHANNEL, b"back"))])

    def test_a_new_address_must_echo_the_relays_cookie_before_it_binds(self):
        credential = rp.member_credential("secret", ALICE)
        (_, answer), = self.core.handle(rp.bind(CHANNEL, credential), "a")
        self.assertEqual(answer[rp.HEADER], rp.CHECK)
        self.assertEqual(self.core.channels, {}, "nothing is bound before the address is proved")
        cookie = answer[rp.HEADER + 1:]
        (_, elsewhere), = self.core.handle(rp.bind(CHANNEL, credential, cookie), "spoofed")
        self.assertEqual(elsewhere[rp.HEADER], rp.CHECK, "a cookie is only good for the address it was sent to")
        (_, answer), = self.core.handle(rp.bind(CHANNEL, credential, cookie), "a")
        self.assertEqual(answer[rp.HEADER], rp.OK)

    def test_a_replayed_bind_from_elsewhere_cannot_move_an_end(self):
        """M1: a captured BIND sent from another address gets a cookie there, and cannot be redone with it."""
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        captured = rp.bind(CHANNEL, rp.member_credential("secret", ALICE))
        (_, answer), = self.core.handle(captured, "attacker")
        self.assertEqual(answer[rp.HEADER], rp.CHECK)
        cookie = answer[rp.HEADER + 1:]
        self.core.handle(captured + cookie, "attacker")  # the proof does not cover the cookie: refused
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"x"), "b"), [("a", rp.deliver(CHANNEL, b"x"))])

    def test_a_tcp_connection_needs_no_cookie(self):
        (_, answer), = self.core.handle(rp.bind(CHANNEL, rp.member_credential("secret", ALICE)), ("tcp", 1),
                                        routable=True)
        self.assertEqual(answer[rp.HEADER], rp.OK)

    def test_a_bad_credential_a_third_user_and_a_stranger_get_nowhere(self):
        self.assertEqual(self.bind(ALICE, "a", secret="wrong"), rp.BAD_TOKEN)
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        self.assertEqual(self.bind(CAROL, "c"), rp.CHANNEL_FULL)
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"sneaky"), "c"), [])
        self.assertEqual(self.core.handle(b"\x99junk", "c"), [])

    def test_the_same_user_from_a_new_port_moves_their_end(self):
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        self.bind(ALICE, "a2")
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"x"), "b"), [("a2", rp.deliver(CHANNEL, b"x"))])
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"y"), "a"), [])

    def test_each_end_is_held_to_its_rate(self):
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        payload = b"x" * 1000
        sent = sum(1 for _ in range(10) if self.core.handle(rp.send(CHANNEL, payload), "a"))
        self.assertEqual(sent, 3)  # a 4000-byte burst holds three 1017-byte datagrams
        self.assertGreater(self.core.stats["limited"], 0)
        self.now[0] += 1.0  # a second later 10 000 more bytes are allowed, up to the burst
        self.assertTrue(self.core.handle(rp.send(CHANNEL, payload), "a"))
        self.assertTrue(self.core.handle(rp.send(CHANNEL, payload), "b"))  # the other end has its own

    def test_a_user_is_held_to_one_rate_over_all_their_channels(self):
        core = rp.RelayCore([rp.HmacVerifier(["secret"])], rate=10**9, burst=10**6, user_rate=40_000,
                            clock=lambda: self.now[0])
        channels = [rp.channel_for("c", bytes([i])) for i in range(8)]
        for channel in channels:
            self.bind(ALICE, "a", channel, core=core)
            self.bind(BOB, ("b", channel), channel, core=core)
        sent = sum(1 for _ in range(10) for c in channels if core.handle(rp.send(c, b"x" * 1000), "a"))
        self.assertLess(sent, 30, "a quarter second of 40 kB/s, not 8 channels' worth")

    def test_a_total_limit_holds_everything_together(self):
        core = rp.RelayCore([rp.HmacVerifier(["secret"])], rate=10**9, burst=10**6, user_rate=None, total_rate=2000,
                            clock=lambda: self.now[0])
        self.bind(ALICE, "a", core=core)
        self.bind(BOB, "b", core=core)
        sent = sum(1 for _ in range(400) if core.handle(rp.send(CHANNEL, b"x" * 1000), "a"))
        self.assertLess(sent, 400)

    def test_an_end_whose_credentials_expire_goes_at_once(self):
        wall = [time.time()]
        core = rp.RelayCore([rp.HmacVerifier(["secret"])], clock=lambda: self.now[0], wall=lambda: wall[0])
        for user, end in ((ALICE, "a"), (BOB, "b")):
            credential = rp.member_credential("secret", user, lifetime=30, now=wall[0])
            (_, answer), = core.handle(rp.bind(CHANNEL, credential, now=wall[0]), end)
            core.handle(rp.bind(CHANNEL, credential, answer[rp.HEADER + 1:], now=wall[0]), end)
        self.assertTrue(core.handle(rp.send(CHANNEL, b"x"), "a"))
        wall[0] += 31
        self.assertEqual(core.handle(rp.send(CHANNEL, b"x"), "a"), [])
        self.assertNotIn(ALICE, core.channels.get(CHANNEL, {}))

    def test_a_secret_that_is_taken_away_drops_its_ends(self):
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        self.core.set_verifiers([rp.HmacVerifier(["the new secret"])])
        self.assertEqual(self.core.channels, {})
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"x"), "a"), [])

    def test_idle_channels_are_forgotten_and_too_many_are_refused(self):
        self.bind(ALICE, "a")
        self.now[0] += rp.IDLE_SECONDS + 1
        self.core.sweep()
        self.assertEqual(self.core.channels, {})
        core = rp.RelayCore([rp.HmacVerifier(["secret"])], max_per_user=2, clock=lambda: self.now[0])
        statuses = [self.bind(ALICE, "a", rp.channel_for("c", bytes([i])), core=core) for i in range(3)]
        self.assertEqual(statuses, [rp.OK, rp.OK, rp.RELAY_FULL])

    def test_too_big_a_datagram_is_not_forwarded(self):
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"x" * (rp.MAX_PAYLOAD + 1)), "a"), [])

    def test_one_address_has_only_so_many_binds_checked_a_second(self):
        message = rp.bind(CHANNEL, rp.member_credential("wrong", ALICE))  # a flood of BINDs that all fail their check
        answered = sum(1 for port in range(200) if self.core.handle(message, ("udp", ("198.51.100.7", port))))
        self.assertEqual(answered, int(rp.BIND_BURST), "changing ports gains nothing")
        self.assertEqual(self.core.stats["throttled"], 200 - int(rp.BIND_BURST))
        self.assertTrue(self.core.handle(message, ("udp", ("198.51.100.8", 1))), "another address is still answered")
        self.now[0] += 1.0
        answered = sum(1 for _ in range(50) if self.core.handle(message, ("udp", ("198.51.100.7", 1))))
        self.assertEqual(answered, int(rp.BIND_RATE), "a second later, a second's worth more")

    def test_answers_are_never_bigger_than_what_was_asked(self):
        """No amplification: a BIND (the smallest is 57 bytes) gets at most a BOUND with a cookie (34)."""
        out = self.core.handle(rp.bind(CHANNEL, rp.member_credential("secret", ALICE)), "a")
        self.assertLessEqual(len(out[0][1]), len(rp.bind(CHANNEL, rp.member_credential("secret", ALICE))))


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.certfile, self.keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.relay = ThreadedRelay(["new secret", "old secret"], host="127.0.0.1", udp_port=0, tls_port=0,
                                   certfile=str(self.certfile), keyfile=str(self.keyfile), handshake_seconds=1.5,
                                   max_per_address=4)
        self.addCleanup(self.relay.close)

    def config(self, user, secret="new secret", tls_only=False):
        return {"host": "127.0.0.1", "port": 9 if tls_only else self.relay.udp_port, "tls_port": self.relay.tls_port,
                "pin": certificate_pin(self.certfile),
                "credential": rp.HmacCredential(rp.member_credential(secret, user))}

    def pair(self, first, second):
        """Two bridges on the same channel; returns their loopback ports and a socket on each."""
        one, two = RelayBridge(first), RelayBridge(second)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        port_one, port_two = one.add(CHANNEL), two.add(CHANNEL)
        self.assertTrue(one.wait_bound(CHANNEL, 10))
        self.assertTrue(two.wait_bound(CHANNEL, 10))
        sockets = []
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(5)
            self.addCleanup(sock.close)
            sockets.append(sock)
        return port_one, port_two, sockets, (one, two)

    def through(self, port_one, port_two, sockets):
        a, b = sockets
        b.sendto(b"hello?", ("127.0.0.1", port_two))  # b's program speaks first, so its bridge knows where to answer
        a.sendto(b"ciphertext from a", ("127.0.0.1", port_one))
        self.assertEqual(b.recvfrom(2048)[0], b"ciphertext from a")
        self.assertEqual(a.recvfrom(2048)[0], b"hello?")

    def tls(self):
        raw = socket.create_connection(("127.0.0.1", self.relay.tls_port), timeout=5)
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        sock = context.wrap_socket(raw)
        self.addCleanup(sock.close)
        return sock

    def closed(self, sock, seconds):
        """True when the relay closes `sock` within `seconds`."""
        sock.settimeout(seconds)
        try:
            return sock.recv(1) == b""
        except (ConnectionError, ssl.SSLError):
            return True
        except socket.timeout:
            return False

    def test_two_pcs_meet_over_udp(self):
        self.through(*self.pair(self.config(ALICE), self.config(BOB))[:3])

    def test_a_pc_that_cannot_send_udp_falls_back_to_tls(self):
        port_one, port_two, sockets, bridges = self.pair(self.config(ALICE, tls_only=True), self.config(BOB))
        self.assertEqual(bridges[0].client.transport, "tls")
        self.assertEqual(bridges[1].client.transport, "udp")
        self.through(port_one, port_two, sockets)

    def test_the_old_secret_still_works_while_members_move_to_the_new_one(self):
        self.through(*self.pair(self.config(ALICE, secret="old secret"), self.config(BOB))[:3])

    def test_dropping_the_old_secret_drops_its_members(self):
        bridges = self.pair(self.config(ALICE, secret="old secret"), self.config(BOB))[3]
        self.relay.loop.call_soon_threadsafe(self.relay.server.set_secrets, ["new secret"])
        self.assertTrue(wait_for(lambda: len(self.relay.server.core.channels.get(CHANNEL, {})) == 1, 3))
        self.assertTrue(wait_for(lambda: bridges[0].client.refused(CHANNEL) == rp.BAD_TOKEN, 20))

    def test_a_wrong_secret_is_refused_in_words(self):
        bridge = RelayBridge(self.config(ALICE, secret="guess"))
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        with self.assertRaisesRegex(rp.RelayError, "did not accept"):
            bridge.wait_bound(CHANNEL, 5)

    def test_without_a_certificate_pin_there_is_no_tls_fallback(self):
        """L4: with nothing to check the relay's certificate against, a man in the middle could stand in for it."""
        config = self.config(ALICE, tls_only=True)
        config["pin"] = ""
        bridge = RelayBridge(config)
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        self.assertFalse(bridge.wait_bound(CHANNEL, 6))
        self.assertEqual(bridge.client.transport, "udp")

    def test_a_wrong_certificate_pin_stops_the_tls_fallback(self):
        config = self.config(ALICE, tls_only=True)
        config["pin"] = "00" * 32
        bridge = RelayBridge(config)
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        self.assertFalse(bridge.wait_bound(CHANNEL, 6))

    def test_a_tls_connection_that_stalls_inside_a_frame_is_closed(self):
        """H1: a length and then nothing used to hold the connection for ever."""
        sock = self.tls()
        sock.sendall(b"\x00\x40")
        self.assertTrue(self.closed(sock, 6))

    def test_a_tls_connection_that_never_binds_is_closed(self):
        """H1: frames that bind nothing do not keep a connection."""
        sock = self.tls()
        ends = time.monotonic() + 4
        while time.monotonic() < ends:
            try:
                sock.sendall(struct.pack("!H", 20) + rp.send(CHANNEL, b"xyz"))
            except OSError:
                break
            time.sleep(0.3)
        self.assertTrue(self.closed(sock, 3))

    def test_an_oversized_frame_closes_the_connection(self):
        sock = self.tls()
        sock.sendall(b"\xff\xff")
        self.assertTrue(self.closed(sock, 3))

    def test_connections_beyond_the_relays_limit_are_closed_at_once(self):
        """MAX_TCP holds the relay's memory under the service's limit (see relay/verify_memory.py)."""
        relay = ThreadedRelay(["new secret"], host="127.0.0.1", udp_port=0, tls_port=0, certfile=str(self.certfile),
                              keyfile=str(self.keyfile), max_tcp=3, handshake_seconds=5)
        self.addCleanup(relay.close)
        held = []
        for number in range(3):
            raw = socket.create_connection(("127.0.0.1", relay.tls_port), timeout=5, source_address=(f"127.0.9.{number + 1}", 0))
            self.addCleanup(raw.close)
            held.append(raw)
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", relay.tls_port), timeout=5, source_address=("127.0.9.9", 0))
        self.addCleanup(extra.close)
        self.assertTrue(self.closed(extra, 3))
        self.assertEqual(relay.server.refused_tcp, 1)
        self.assertFalse(self.closed(held[0], 0.5), "the ones within the limit are kept")

    def test_one_address_cannot_hold_many_connections(self):
        """H1: a cap per address, counted from the moment a connection opens (before its TLS handshake)."""
        held = []
        for _ in range(4):
            raw = socket.create_connection(("127.0.0.1", self.relay.tls_port), timeout=5)
            self.addCleanup(raw.close)
            held.append(raw)
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", self.relay.tls_port), timeout=5)
        self.addCleanup(extra.close)
        self.assertTrue(self.closed(extra, 3))
        self.assertTrue(self.closed(held[0], 4), "a connection that never finishes its handshake is closed too")

    def test_a_member_on_ipv6_and_one_on_ipv4_meet_through_a_dual_stack_relay(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6 on this PC")
        relay = ThreadedRelay(["new secret"], udp_port=0)  # the default host: both families
        self.addCleanup(relay.close)
        v6 = {"host": "::1", "port": relay.udp_port, "credential": rp.HmacCredential(rp.member_credential("new secret", ALICE))}
        v4 = {"host": "127.0.0.1", "port": relay.udp_port,
              "credential": rp.HmacCredential(rp.member_credential("new secret", BOB))}
        self.through(*self.pair(v6, v4)[:3])

    def test_a_relay_name_with_both_families_is_reached_on_the_one_that_answers(self):
        from privatepartynet import relayclient

        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dead.bind(("127.0.0.1", 0))
        self.addCleanup(dead.close)  # an address that takes BINDs and never answers, like a blocked family
        resolver = lambda host, port: [("127.0.0.1", dead.getsockname()[1]), ("127.0.0.1", self.relay.udp_port)]  # noqa
        config = self.config(ALICE)
        config["host"] = "relay.example"
        bridge = relayclient.RelayBridge(config, resolver=resolver)
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        self.assertTrue(bridge.wait_bound(CHANNEL, 5))
        self.assertEqual(bridge.client.address, ("127.0.0.1", self.relay.udp_port))

    def test_the_command_line_makes_a_secret(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(relay_main(["--new-secret"]), 0)
        self.assertEqual(len(out.getvalue().strip()), 32)


if __name__ == "__main__":
    unittest.main()
