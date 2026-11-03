"""The relay: tokens, the forwarding rules and limits, the server over UDP and over TLS, and the PC's side of it."""

import base64
import hashlib
import hmac
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet import relayproto as rp
from privatepartynet.cert import ensure_certificate
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


class TokenTests(unittest.TestCase):
    def test_a_token_names_its_user_until_it_expires(self):
        token = rp.mint_token("secret", ALICE, lifetime=60, now=1000)
        self.assertEqual(len(token), rp.TOKEN_BYTES)
        self.assertEqual(rp.check_token("secret", token, now=1030), ALICE)
        self.assertIsNone(rp.check_token("secret", token, now=1061))

    def test_a_token_from_another_secret_or_changed_is_refused(self):
        token = rp.mint_token("secret", ALICE)
        self.assertIsNone(rp.check_token("other", token))
        changed = token[:4] + bytes.fromhex(BOB) + token[20:]
        self.assertIsNone(rp.check_token("secret", changed))
        self.assertIsNone(rp.check_token("secret", token[:-1]))

    def test_turn_credentials_are_cotunrs_rest_form(self):
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


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.core = rp.RelayCore(["secret"], rate=10_000, burst=4_000, clock=lambda: self.now[0])

    def bind(self, user, end, channel=CHANNEL, secret="secret"):
        out = self.core.handle(rp.bind(channel, rp.mint_token(secret, user)), end)
        self.assertEqual(out[0][0], end)
        return out[0][1][-1]

    def test_two_ends_of_a_channel_reach_each_other(self):
        self.assertEqual(self.bind(ALICE, "a"), rp.OK)
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"early"), "a"), [])  # nobody at the other end yet
        self.assertEqual(self.bind(BOB, "b"), rp.OK)
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"hello"), "a"), [("b", rp.deliver(CHANNEL, b"hello"))])
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"back"), "b"), [("a", rp.deliver(CHANNEL, b"back"))])

    def test_a_bad_token_a_third_user_and_a_stranger_get_nowhere(self):
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

    def test_a_total_limit_holds_everything_together(self):
        core = rp.RelayCore(["secret"], rate=10**9, burst=10**6, total_rate=2000, clock=lambda: self.now[0])
        core.handle(rp.bind(CHANNEL, rp.mint_token("secret", ALICE)), "a")
        core.handle(rp.bind(CHANNEL, rp.mint_token("secret", BOB)), "b")
        sent = sum(1 for _ in range(400) if core.handle(rp.send(CHANNEL, b"x" * 1000), "a"))
        self.assertLess(sent, 400)

    def test_idle_channels_are_forgotten_and_too_many_are_refused(self):
        self.bind(ALICE, "a")
        self.now[0] += rp.IDLE_SECONDS + 1
        self.core.sweep()
        self.assertEqual(self.core.channels, {})
        core = rp.RelayCore(["secret"], max_per_user=2, clock=lambda: self.now[0])
        statuses = [core.handle(rp.bind(rp.channel_for("c", bytes([i])), rp.mint_token("secret", ALICE)), "a")[0][1][-1]
                    for i in range(3)]
        self.assertEqual(statuses, [rp.OK, rp.OK, rp.RELAY_FULL])

    def test_too_big_a_datagram_is_not_forwarded(self):
        self.bind(ALICE, "a")
        self.bind(BOB, "b")
        self.assertEqual(self.core.handle(rp.send(CHANNEL, b"x" * (rp.MAX_PAYLOAD + 1)), "a"), [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.certfile, self.keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.relay = ThreadedRelay(["new secret", "old secret"], host="127.0.0.1", udp_port=0, tls_port=0,
                                   certfile=str(self.certfile), keyfile=str(self.keyfile))
        self.addCleanup(self.relay.close)

    def config(self, user, secret="new secret", tls_only=False):
        return {"host": "127.0.0.1", "port": 9 if tls_only else self.relay.udp_port, "tls_port": self.relay.tls_port,
                "pin": certificate_pin(self.certfile), "token": rp.mint_token(secret, user)}

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
        a.settimeout(5)
        self.assertEqual(a.recvfrom(2048)[0], b"hello?")

    def test_two_pcs_meet_over_udp(self):
        self.through(*self.pair(self.config(ALICE), self.config(BOB))[:3])

    def test_a_pc_that_cannot_send_udp_falls_back_to_tls(self):
        port_one, port_two, sockets, bridges = self.pair(self.config(ALICE, tls_only=True), self.config(BOB))
        self.assertEqual(bridges[0].client.transport, "tls")
        self.assertEqual(bridges[1].client.transport, "udp")
        self.through(port_one, port_two, sockets)

    def test_the_old_secret_still_works_while_tokens_move_to_the_new_one(self):
        self.through(*self.pair(self.config(ALICE, secret="old secret"), self.config(BOB))[:3])

    def test_a_wrong_secret_is_refused_in_words(self):
        bridge = RelayBridge(self.config(ALICE, secret="guess"))
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        with self.assertRaisesRegex(rp.RelayError, "did not accept"):
            bridge.wait_bound(CHANNEL, 5)

    def test_a_wrong_certificate_pin_stops_the_tls_fallback(self):
        config = self.config(ALICE, tls_only=True)
        config["pin"] = "00" * 32
        bridge = RelayBridge(config)
        self.addCleanup(bridge.close)
        bridge.add(CHANNEL)
        self.assertFalse(bridge.wait_bound(CHANNEL, 6))

    def test_the_command_line_makes_a_secret(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(relay_main(["--new-secret"]), 0)
        self.assertEqual(len(out.getvalue().strip()), 32)


if __name__ == "__main__":
    unittest.main()
