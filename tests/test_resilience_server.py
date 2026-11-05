"""Bad internet does not throw anyone out (3.3), on the host's side: the heartbeat and each member's link state, the
reconnect grace, coming back with a resume token, and the chat missed meanwhile. The clock is the test's own, so a
two-minute grace takes no time."""

import json
import time
import unittest

from privatepartynet import linkstate
from privatepartynet.identity import Identity
from privatepartynet.linkstate import LinkMeter, path_of
from privatepartynet.server import ask_message, resume_message
from test_server import read_until
from test_server_servers import ServerTestCase


class Clock:
    def __init__(self):
        self.now, self.wall = 1000.0, 1_800_000_000.0

    def tick(self, seconds):
        self.now += seconds
        self.wall += seconds

    def clock(self):
        return (lambda: self.now), (lambda: self.wall)


class LinkMeterTests(unittest.TestCase):
    def test_one_missed_answer_means_nothing_and_silence_moves_the_state_on(self):
        meter, now = LinkMeter(0.0, 0.0), 0.0
        for second in range(0, 40, 2):  # answered every 2 s, but one in three is lost
            ping = meter.ping(second)
            if second % 6:
                meter.pong(ping, second + 0.05)
            self.assertFalse(meter.update(second + 0.1, second + 0.1), f"still ok at {second} s")
        self.assertEqual(meter.state, "ok")
        self.assertAlmostEqual(meter.loss, 0.33, delta=0.1)
        self.assertEqual(meter.public()["rtt"], 50)
        now = 40.0
        self.assertTrue(meter.update(now + 4.5, now + 4.5))
        self.assertEqual(meter.state, "unstable")
        self.assertTrue(meter.update(now + 10.5, now + 10.5))
        self.assertEqual((meter.state, meter.public()["rtt"]), ("reconnecting", None))

    def test_back_to_ok_only_after_three_good_heartbeats(self):
        meter = LinkMeter(0.0, 0.0)
        meter.update(12.0, 12.0)
        self.assertEqual(meter.state, "reconnecting")
        for n, at in enumerate((12.0, 14.0, 16.0)):
            meter.pong(meter.ping(at), at + 0.03)
            meter.update(at + 0.1, at + 0.1)
            self.assertEqual(meter.state, "ok" if n == 2 else "reconnecting")
        self.assertEqual(meter.since, 16.1)

    def test_the_path_is_named_from_the_address_the_host_sees(self):
        self.assertEqual(path_of("192.168.0.12"), "direct-lan")
        self.assertEqual(path_of("26.10.20.30"), "direct-lan", "a VPN both PCs are on")
        self.assertEqual(path_of("2a00:1450:4001::5"), "direct-ipv6")
        self.assertEqual(path_of("1.2.3.4"), "direct")
        self.assertEqual(path_of("127.1.4.9"), "punched")
        self.assertEqual(path_of("127.1.4.9", {"127.1.4.9": "relay"}), "relay")
        self.assertIsNone(path_of("127.0.0.1"))


class Base(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.time = Clock()

    def party(self, **options):
        options.setdefault("clock", self.time.clock())
        options.setdefault("ping_seconds", 3600)  # the test runs each pulse itself
        return super().party(**options)

    def joined(self, party, name="Alex", identity=None):
        """A member who joined with an identity, as through the app: (member id, session, identity, resume token)."""
        identity = identity or Identity.create()
        token = party.join_token(name, user=identity.user_id)
        resume = party._join_resume[token]  # what GET /ask?ticket= hands the app with the allowed answer
        session = party.redeem(token)
        return party.member_for(session), session, identity, resume

    def entry(self, party, member_id):
        return next((m for m in party.roster() if m["id"] == member_id), None)

    def resume(self, party, resume, identity):
        response = self.open(party, "GET", "/ask?challenge=1")
        nonce = json.loads(response.read())["nonce"]
        body = {"token": resume, "card": identity.card("Alex"), "nonce": nonce,
                "sig": identity.sign(resume_message(party.fingerprint, nonce, resume)).hex()}
        response = self.open(party, "POST", "/resume", json.dumps(body), {"Content-Type": "application/json"})
        return response.status, response.read()


class GraceTests(Base):
    def test_a_dropout_keeps_the_member_their_role_and_seat_until_the_grace_runs_out(self):
        party = self.party(reconnect_grace=120)
        alex, session, _, _ = self.joined(party)
        stream = self.stream(party, session)
        read_until(stream, "hello")
        party.join_voice(alex, "voice-1")
        party.pulse()
        ping = read_until(stream, "ping")
        self.assertEqual(self.post(party, "/pong", {"id": ping["id"]}, session)[0], 200)
        self.assertEqual(self.entry(party, alex)["link"]["state"], "ok")
        stream.close()  # the connection drops
        for seconds, state in ((3, "ok"), (2, "unstable"), (6, "reconnecting"), (100, "reconnecting")):
            self.time.tick(seconds)
            party.pulse()
            entry = self.entry(party, alex)
            self.assertEqual((entry["link"]["state"], entry["voice"], entry["online"]), (state, "voice-1", True))
        self.assertEqual(list(party._log.get("general", ())), [], "nobody 'left' or 'joined': no chat noise")
        self.time.tick(10)
        party.pulse()
        self.assertIsNone(self.entry(party, alex), "out of the party once the grace ran out")
        self.assertEqual(self.get(party, "/link", session)[0], 401)

    def test_everyone_sees_the_links_every_heartbeat(self):
        party = self.party()
        alex, session, _, _ = self.joined(party)
        stream = self.stream(party, session)
        read_until(stream, "hello")
        roster = read_until(stream, "roster")
        self.assertEqual(set(roster["members"][-1]["link"]), {"state", "rtt", "loss", "since"})
        party.pulse()
        links = read_until(stream, "links")
        self.assertEqual(links["links"][party.host_id]["state"], "ok")
        self.assertIn(alex, links["links"])

    def test_closing_the_window_says_goodbye_and_skips_the_grace(self):
        party = self.party()
        alex, session, _, _ = self.joined(party)
        stream = self.stream(party, session)
        read_until(stream, "hello")
        self.assertEqual(self.post(party, "/bye", {}, session)[0], 202)
        stream.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.entry(party, alex)["online"]:
            time.sleep(0.05)
        self.assertFalse(self.entry(party, alex)["online"])


class ResumeTests(Base):
    def drop(self, party, session, seconds=11):
        stream = self.stream(party, session)
        read_until(stream, "hello")
        stream.close()
        self.time.tick(seconds)
        party.pulse()

    def test_after_ten_seconds_away_only_a_signed_resume_lets_the_page_back_in(self):
        party = self.party()
        alex, session, identity, resume = self.joined(party)
        self.drop(party, session)
        self.assertEqual(self.entry(party, alex)["link"]["state"], "reconnecting")
        self.assertEqual(self.get(party, "/link", session)[0], 409, "the cookie alone is not enough now")
        self.assertEqual(self.open(party, "GET", "/events", headers={"Cookie": f"privatepartynet_member={session}"}).status, 409)
        status, body = self.resume(party, resume, identity)
        self.assertEqual((status, json.loads(body)["ok"]), (200, True))
        self.assertEqual(self.get(party, "/link", session)[0], 200)
        read_until(self.stream(party, session), "hello")

    def test_a_short_blip_needs_no_resume(self):
        party = self.party()
        alex, session, _, _ = self.joined(party)
        self.drop(party, session, seconds=5)
        self.assertEqual(self.entry(party, alex)["link"]["state"], "unstable")
        read_until(self.stream(party, session), "hello")

    def test_someone_else_cannot_resume_a_member(self):
        party = self.party()
        _, session, _, resume = self.joined(party)
        self.drop(party, session)
        self.assertEqual(self.resume(party, resume, Identity.create())[0], 403)

    def test_a_banned_or_kicked_members_token_dies_at_once(self):
        for action in ("ban", "kick"):
            with self.subTest(action):
                party = self.party()
                alex, session, identity, resume = self.joined(party)
                self.drop(party, session)
                getattr(party, action)(party.host_id, alex)
                self.assertEqual(self.resume(party, resume, identity)[0], 403)

    def test_a_revoked_or_expired_token_is_refused(self):
        party = self.party(reconnect_grace=60)
        alex, session, identity, resume = self.joined(party)
        party.revoke_resume(alex)
        self.drop(party, session)
        self.assertEqual(self.resume(party, resume, identity)[0], 403)
        party2 = self.party(reconnect_grace=60, server_id="srv2")
        _, session2, identity2, resume2 = self.joined(party2)
        self.drop(party2, session2, seconds=61)
        self.assertEqual(self.resume(party2, resume2, identity2)[0], 403, "the grace is over")

    def test_the_join_answer_hands_the_app_its_resume_token(self):
        party = self.party()
        identity = Identity.create()
        self.joined(party, identity=identity)  # known now: the next ask comes straight in
        nonce = party.challenge()
        ticket = party.ask("Alex", "", "203.0.113.5", identity.card("Alex"), nonce,
                           identity.sign(ask_message(party.fingerprint, nonce, "Alex")).hex())
        answer = party.answer(ticket)
        self.assertEqual(answer["state"], "allowed")
        self.assertTrue(answer["resume"])


class CatchUpTests(Base):
    def test_chat_missed_during_a_dropout_arrives_when_the_page_comes_back(self):
        party = self.party()
        alex, session, identity, resume = self.joined(party)
        sam, _, _, _ = self.joined(party, "Sam")
        party.chat(sam, "before")
        stream = self.stream(party, session)
        history = read_until(stream, "history", lambda e: e["channel"] == "general")
        seen = history["messages"][-1]["seq"]
        stream.close()
        for n in range(3):
            party.chat(sam, f"while away {n}")
        self.time.tick(12)
        party.pulse()
        self.assertEqual(self.resume(party, resume, identity)[0], 200)
        response = self.open(party, "GET", f"/events?since=general:{seen}",
                             headers={"Cookie": f"privatepartynet_member={session}"})
        caught = read_until(response, "history", lambda e: e["channel"] == "general")
        self.assertEqual(caught["since"], seen)
        self.assertEqual([m["text"] for m in caught["messages"]], [f"while away {n}" for n in range(3)])

    def test_more_missed_than_is_kept_sends_the_whole_history(self):
        party = self.party()
        alex, session, _, _ = self.joined(party)
        sam, _, _, _ = self.joined(party, "Sam")
        for n in range(205):
            party.chat(sam, f"m{n}")
        response = self.open(party, "GET", "/events?since=general:1",
                             headers={"Cookie": f"privatepartynet_member={session}"})
        caught = read_until(response, "history", lambda e: e["channel"] == "general")
        self.assertNotIn("since", caught, "too much was missed to add on: the page replaces what it has")


class GameNetworkGraceTests(Base):
    def test_a_dropped_game_link_keeps_the_member_on_the_list_until_they_are_out(self):
        party = self.party(reconnect_grace=60)
        alex, session, identity, _ = self.joined(party)
        party.attach(alex)
        directory = party.net_directory()
        key = "ab" * 32
        with party._lock:
            party._net_links[alex] = (stop := __import__("threading").Event(), identity.user_id, key)
        directory.join(identity.user_id, key)
        party.net_closed(alex, stop)
        self.assertIn(identity.user_id, {e["user"] for e in directory.peers()}, "still on the list through the dropout")
        party.detach(alex, party._members[alex]["queue"])
        self.time.tick(61)
        party.pulse()
        self.assertNotIn(identity.user_id, {e["user"] for e in directory.peers()}, "off it once they are out")


if __name__ == "__main__":
    unittest.main()
