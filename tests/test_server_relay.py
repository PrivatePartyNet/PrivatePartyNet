"""The group's relay server, as the party keeps and hands it out: only the owner sets it, the secret never leaves the
owner's PC, members get credentials of their own (in settings the owner signs) and their pages TURN credentials."""

import base64
import hashlib
import hmac
import time

from privatepartynet import relayproto as rp
from privatepartynet.identity import Identity
from privatepartynet.pathway import PassCredential, PassVerifier, invite_key, open_relay_config
from privatepartynet.server import Forbidden, clean_relay
from test_server import read_until
from test_server_servers import ServerTestCase

SECRET = "a-relay-secret-of-some-length"
RELAY = {"host": "relay.example.net", "port": 3479, "tls_port": 443, "pin": "ab" * 32, "secret": SECRET}
MEMBER = "11" * 16


class RelaySettingsTests(ServerTestCase):
    def test_only_the_owner_sets_the_relay(self):
        party = self.party()
        admin, _ = self.member(party, "Ada", user="u-ada", role="admin")
        with self.assertRaises(Forbidden):
            party.set_relay(admin, RELAY)
        self.assertTrue(party.set_relay(party.host_id, RELAY))
        self.assertEqual(party.relay_view()["host"], "relay.example.net")

    def test_the_view_never_shows_the_secret(self):
        party = self.party()
        party.set_relay(party.host_id, RELAY)
        self.assertNotIn(SECRET, repr(party.relay_view()))

    def test_bad_settings_are_refused_in_words(self):
        for change, words in (({"host": ""}, "address"), ({"host": "a b"}, "address"), ({"port": 0}, "ports"),
                              ({"port": "x"}, "numbers"), ({"secret": "short"}, "16 to 200"),
                              ({"pin": "1234"}, "64 letters")):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, words):
                    clean_relay({**RELAY, **change})
        self.assertEqual(clean_relay({**RELAY, "pin": "AB:" * 31 + "AB"})["pin"], "ab" * 32)

    def test_the_relay_is_kept_with_the_server_and_can_be_removed(self):
        first = self.party()
        first.set_relay(first.host_id, RELAY)
        first.stop()
        second = self.party()
        self.assertEqual(second.relay_view()["host"], "relay.example.net")
        second.set_relay(second.host_id, None)
        self.assertIsNone(second.relay_view())


def check(verifier, message):
    _, channel, body = rp.parse(message)
    return verifier.check(channel, body, time.time())


class MemberRelayTests(ServerTestCase):
    def test_a_member_gets_their_own_credentials_signed_by_the_owner_and_not_the_secret(self):
        owner, member = Identity.create(), Identity.create()
        party = self.party(owner=owner)
        party.set_relay(party.host_id, RELAY)
        signed = party.path_relay_for(member.user_id, member.sign_public.hex())
        self.assertNotIn(SECRET, repr(signed))
        config = open_relay_config(signed, owner.sign_public.hex())
        self.assertIsNotNone(config, "the owner's signature checks out")
        vps = config["vps"][0]
        self.assertEqual((vps["host"], vps["port"], vps["tls_port"], vps["pin"]), ("relay.example.net", 3479, 443, "ab" * 32))
        bind = rp.HmacCredential(bytes.fromhex(vps["credential"])).bind(rp.channel_for("t", b"k"))
        self.assertEqual(check(rp.HmacVerifier([SECRET]), bind)[0], member.user_id)
        self.assertGreater(config["expires"], time.time() + 3600)
        self.assertLessEqual(config["expires"], time.time() + rp.TOKEN_SECONDS + 5, "short-lived")
        passed = PassCredential(bytes.fromhex(config["friend"]["pass"]), member.sign).bind(rp.channel_for("t", b"k"))
        self.assertEqual(check(PassVerifier(config["friend"]["owner"]), passed)[0], member.user_id)

    def test_without_the_members_key_there_is_no_pass_and_without_an_owner_no_friend_relays(self):
        party = self.party(owner=Identity.create())
        config = party.path_relay_for(MEMBER)["relay"]
        self.assertEqual(config["vps"], [])
        self.assertEqual(config["friend"]["pass"], "")
        self.assertNotIn("friend", self.party(server_id="srv2", owner=None).path_relay_for(MEMBER)["relay"])

    def test_an_invite_carries_the_relays_with_credentials_for_its_own_id(self):
        owner = Identity.create()
        party = self.party(owner=owner)
        party.set_relay(party.host_id, RELAY)
        party.friend_relays = lambda: [("198.51.100.7", 40001)]
        secret = bytes(16)
        relays = party.invite_relays(secret)
        self.assertEqual([r[0] for r in relays], ["vps", "friend"])
        sign, _, joiner = invite_key(secret)
        channel = rp.channel_for("t", b"k")
        self.assertEqual(check(rp.HmacVerifier([SECRET]), rp.bind(channel, bytes.fromhex(relays[0][5])))[0], joiner)
        friend = PassCredential(bytes.fromhex(relays[1][5]), sign).bind(channel)
        self.assertEqual(check(PassVerifier(owner.sign_public), friend)[0], joiner)
        hosts = party.host_relays()
        self.assertEqual([(h["host"], h["port"]) for h in hosts], [("relay.example.net", 3479), ("198.51.100.7", 40001)])
        self.assertIsNotNone(check(rp.HmacVerifier([SECRET]), hosts[0]["credential"].bind(channel)))
        self.assertIsNotNone(check(PassVerifier(owner.sign_public), hosts[1]["credential"].bind(channel)))


class TurnTests(ServerTestCase):
    def test_pages_get_turn_credentials_of_their_own_once_a_relay_is_set(self):
        party = self.party()
        _, session = self.member(party, "Alex", user="aa" * 16)
        stream = self.stream(party, session)
        self.assertEqual(read_until(stream, "hello")["turn"], [])
        party.set_relay(party.host_id, RELAY)
        ice = read_until(stream, "ice")
        turn = ice["turn"][0]
        self.assertEqual(turn["urls"], ["turn:relay.example.net:3478?transport=udp",
                                        "turn:relay.example.net:3478?transport=tcp"])
        expiry, user = turn["username"].split(":")
        self.assertEqual(user, "aa" * 16)
        self.assertGreater(int(expiry), time.time() + 3600)
        wanted = base64.b64encode(hmac.new(SECRET.encode(), turn["username"].encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(turn["credential"], wanted, "what coturn's use-auth-secret checks")
        self.assertNotIn(SECRET, repr(ice))

    def test_a_page_that_connects_later_gets_them_in_its_hello(self):
        party = self.party()
        party.set_relay(party.host_id, RELAY)
        _, session = self.member(party, "Sam", user="bb" * 16)
        hello = read_until(self.stream(party, session), "hello")
        self.assertEqual(hello["turn"][0]["username"].split(":")[1], "bb" * 16)

    def test_turn_can_be_left_off(self):
        party = self.party()
        party.set_relay(party.host_id, {**RELAY, "turn": False})
        _, session = self.member(party, "Sam")
        self.assertEqual(read_until(self.stream(party, session), "hello")["turn"], [])

    def test_a_relay_given_at_both_its_addresses_reaches_members_on_either(self):
        party = self.party()
        party.set_relay(party.host_id, {**RELAY, "host": "198.51.100.9, 2001:db8::5"})
        self.assertEqual([v["host"] for v in party.path_relay_for(MEMBER)["relay"]["vps"]], ["198.51.100.9", "2001:db8::5"])
        self.assertEqual([r[1] for r in party.invite_relays(bytes(16))], ["198.51.100.9", "2001:db8::5"])
        self.assertEqual([r["host"] for r in party.host_relays()], ["198.51.100.9", "2001:db8::5"])
        _, session = self.member(party, "Sam")
        urls = read_until(self.stream(party, session), "hello")["turn"][0]["urls"]
        self.assertEqual(urls[0], "turn:198.51.100.9:3478?transport=udp")
        self.assertEqual(urls[2], "turn:[2001:db8::5]:3478?transport=udp")
        with self.assertRaisesRegex(ValueError, "comma"):
            clean_relay({**RELAY, "host": "a, b, c"})

    def test_an_ipv6_relay_address_is_bracketed(self):
        party = self.party()
        party.set_relay(party.host_id, {**RELAY, "host": "2001:db8::5"})
        _, session = self.member(party, "Sam")
        urls = read_until(self.stream(party, session), "hello")["turn"][0]["urls"]
        self.assertTrue(urls[0].startswith("turn:[2001:db8::5]:3478"))
