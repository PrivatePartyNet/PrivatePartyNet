"""The persistent-server side of a party: saved chat, channels, roles, bans, join requests and voice presence."""

import http.client
import json
import ssl
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet.cert import ensure_certificate
from privatepartynet.server import MAX_VOICE, Forbidden, NotFound, Party
from privatepartynet.store import Store
from privatepartynet.vault import Vault
from test_server import JSON_HEADERS, insecure_context, read_until

FAST = {"n": 2 ** 10, "r": 8, "p": 1}


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.certfile, self.keyfile = ensure_certificate(self.folder, ["localhost", "127.0.0.1"])
        self.vault = Vault(self.folder / "keys", scrypt_params=FAST)
        self.store = Store(self.vault, self.folder / "store")
        self.addCleanup(self.store.close)
        self.parties = []

    def party(self, **options):
        options.setdefault("store", self.store)
        options.setdefault("server_id", "srv1")
        options.setdefault("host_user_id", "owner-user")
        options.setdefault("trust_local", False)  # the tests come from 127.0.0.1; most of them check the password
        party = Party("428617", 0, self.certfile, self.keyfile, party_name="Heroes night", host_name="Host",
                      failed_login_delay=0, heartbeat=0.2, **options)
        party.start()
        self.parties.append(party)
        self.addCleanup(party.stop)
        return party

    def member(self, party, name, user=None, role="member"):
        """Join someone directly (no HTTP) and return (member id, session token)."""
        session = party.redeem(party.join_token(name, role=role, user=user))
        self.assertIsNotNone(session, f"{name} could not join")
        return party.member_for(session), session

    def host(self, party):
        return party.host_id

    # HTTP helpers for the routes
    def open(self, party, method, path, body=None, headers=None):
        conn = http.client.HTTPSConnection("127.0.0.1", party.port, timeout=5, context=insecure_context())
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        return conn.getresponse()

    def post(self, party, path, body, session, cookie="privatepartynet_member"):
        response = self.open(party, "POST", path, json.dumps(body), {**JSON_HEADERS, "Cookie": f"{cookie}={session}"})
        data = response.read()
        return response.status, data

    def get(self, party, path, session, cookie="privatepartynet_member"):
        response = self.open(party, "GET", path, headers={"Cookie": f"{cookie}={session}"})
        data = response.read()
        return response.status, data

    def stream(self, party, session, cookie="privatepartynet_member"):
        response = self.open(party, "GET", "/events", headers={"Cookie": f"{cookie}={session}"})
        self.assertEqual(response.status, 200)
        return response


class SavedChatTests(ServerTestCase):
    def test_a_chat_between_two_people_is_kept_for_someone_who_joins_later(self):
        party = self.party()
        alex, _ = self.member(party, "Alex")
        party.chat(alex, "just us")
        _, late = self.member(party, "Sam")
        history = read_until(self.stream(party, late), "history")
        self.assertEqual([m["text"] for m in history["messages"]], ["just us"])
        self.assertEqual(history["channel"], "general")

    def test_chat_survives_the_server_being_closed_and_opened_again(self):
        first = self.party()
        alex, _ = self.member(first, "Alex", user="u-alex")
        first.chat(alex, "one")
        first.chat(alex, "two")
        first.stop()
        second = self.party()
        _, session = self.member(second, "Alex", user="u-alex")
        history = read_until(self.stream(second, session), "history")
        self.assertEqual([m["text"] for m in history["messages"]], ["one", "two"])
        self.assertEqual([m["seq"] for m in history["messages"]], [1, 2])
        later = second.chat(second.member_for(session), "three")
        self.assertEqual(later["seq"], 3, "numbering carries on where it stopped")

    def test_messages_are_written_to_the_store_with_the_senders_permanent_id(self):
        party = self.party()
        alex, _ = self.member(party, "Alex", user="u-alex")
        party.chat(alex, "saved")
        (record,) = self.store.history("srv:srv1:general")
        self.assertEqual((record["sender"], record["seq"], record["body"]["text"]), ("u-alex", 1, "saved"))

    def test_without_a_store_chat_still_works_for_the_run(self):
        party = self.party(store=None)
        alex, _ = self.member(party, "Alex")
        self.assertEqual(party.chat(alex, "hi")["seq"], 1)
        self.assertEqual(party.channel_history(alex, "general")[0]["text"], "hi")

    def test_older_pages_come_back_oldest_first(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        for number in range(1, 121):
            party.chat(alex, f"m{number}")
        status, data = self.get(party, "/history?channel=general&limit=50", session)
        self.assertEqual(status, 200)
        page = json.loads(data)["messages"]
        self.assertEqual([m["text"] for m in page][:2], ["m71", "m72"])
        self.assertEqual(page[-1]["text"], "m120")
        status, data = self.get(party, f"/history?channel=general&limit=50&before={page[0]['ts']}", session)
        older = json.loads(data)["messages"]
        self.assertEqual(older[-1]["text"], "m70")
        self.assertEqual(len(older), 50)

    def test_a_connecting_member_gets_the_newest_two_hundred_and_can_page_for_the_rest(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        for number in range(250):
            party.chat(alex, f"m{number}")
        history = read_until(self.stream(party, session), "history")
        self.assertEqual(len(history["messages"]), 200)
        self.assertEqual(history["messages"][0]["text"], "m50")

    def test_history_needs_a_session_and_a_text_channel(self):
        party = self.party()
        status, _ = self.get(party, "/history?channel=general", "nope")
        self.assertEqual(status, 401)
        _, session = self.member(party, "Alex")
        status, _ = self.get(party, "/history?channel=voice-1", session)
        self.assertEqual(status, 404)


class ChannelTests(ServerTestCase):
    def test_a_new_server_has_a_text_and_a_voice_channel(self):
        party = self.party()
        self.assertEqual([(c["id"], c["kind"]) for c in party.channels()], [("general", "text"), ("voice-1", "voice")])

    def test_hello_lists_the_channels_and_the_server_id(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        hello = read_until(self.stream(party, session), "hello")
        self.assertEqual(hello["server"], "srv1")
        self.assertEqual([c["name"] for c in hello["channels"]], ["general", "Voice 1"])

    def test_the_owner_makes_a_channel_and_everyone_is_told(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        stream = self.stream(party, session)
        read_until(stream, "hello")
        channel = party.add_channel(party.host_id, "  strategy   talk ", "text")
        self.assertEqual(channel["name"], "strategy talk")
        update = read_until(stream, "channels")
        self.assertIn("strategy talk", [c["name"] for c in update["channels"]])

    def test_chat_goes_to_the_channel_named_and_is_kept_there(self):
        party = self.party()
        channel = party.add_channel(party.host_id, "maps")
        alex, session = self.member(party, "Alex")
        status, _ = self.post(party, "/chat", {"text": "a map", "channel": channel["id"]}, session)
        self.assertEqual(status, 202)
        saved = self.store.history(f"srv:srv1:{channel['id']}")
        self.assertEqual([r["body"]["text"] for r in saved], ["a map"])
        self.assertEqual(self.store.count("srv:srv1:general"), 0)

    def test_a_voice_channel_takes_no_text_and_an_unknown_channel_is_refused(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        self.assertEqual(self.post(party, "/chat", {"text": "hi", "channel": "voice-1"}, session)[0], 404)
        self.assertEqual(self.post(party, "/chat", {"text": "hi", "channel": "nope"}, session)[0], 404)

    def test_an_ordinary_member_cannot_manage_channels(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        with self.assertRaises(Forbidden):
            party.add_channel(alex, "mine")
        status, _ = self.post(party, "/channel", {"action": "add", "name": "mine"}, session)
        self.assertEqual(status, 403)

    def test_the_last_text_channel_cannot_be_removed_but_a_voice_one_can(self):
        party = self.party()
        with self.assertRaises(Forbidden):
            party.remove_channel(party.host_id, "general")
        party.remove_channel(party.host_id, "voice-1")
        self.assertEqual([c["id"] for c in party.channels()], ["general"])
        extra = party.add_channel(party.host_id, "second")
        party.remove_channel(party.host_id, "general")
        self.assertEqual([c["id"] for c in party.channels()], [extra["id"]])

    def test_removed_channels_keep_their_saved_messages(self):
        party = self.party()
        extra = party.add_channel(party.host_id, "side")
        alex, _ = self.member(party, "Alex")
        party.chat(alex, "remember me", extra["id"])
        party.remove_channel(party.host_id, extra["id"])
        self.assertEqual(self.store.count(f"srv:srv1:{extra['id']}"), 1)

    def test_channels_are_remembered_when_the_server_reopens(self):
        first = self.party()
        first.add_channel(first.host_id, "lore")
        first.rename_channel(first.host_id, "general", "lobby")
        first.stop()
        second = self.party()
        self.assertEqual([c["name"] for c in second.channels()], ["lobby", "Voice 1", "lore"])

    def test_names_are_cleaned_and_the_count_is_limited(self):
        party = self.party()
        with self.assertRaises(NotFound):
            party.add_channel(party.host_id, "   ")
        long_name = party.add_channel(party.host_id, "x" * 100)
        self.assertEqual(len(long_name["name"]), 32)


class RoleTests(ServerTestCase):
    def test_the_owner_can_make_an_admin_and_it_is_remembered(self):
        first = self.party()
        alex, _ = self.member(first, "Alex", user="u-alex")
        first.set_role(first.host_id, alex, "admin")
        first.stop()
        second = self.party()
        again, _ = self.member(second, "Alex", user="u-alex")
        roster = {m["name"]: m["role"] for m in second.roster()}
        self.assertEqual(roster["Alex"], "admin")

    def test_an_admin_can_remove_members_but_not_the_owner_or_another_admin(self):
        party = self.party()
        admin, _ = self.member(party, "Ada", user="u-ada")
        other_admin, _ = self.member(party, "Ben", user="u-ben")
        member, _ = self.member(party, "Cy", user="u-cy")
        party.set_role(party.host_id, admin, "admin")
        party.set_role(party.host_id, other_admin, "admin")
        self.assertFalse(party.kick(admin, party.host_id))
        self.assertFalse(party.kick(admin, other_admin))
        self.assertTrue(party.kick(admin, member))

    def test_an_admin_cannot_make_admins_but_can_switch_members_and_guests(self):
        party = self.party()
        admin, _ = self.member(party, "Ada", user="u-ada")
        member, _ = self.member(party, "Cy", user="u-cy")
        party.set_role(party.host_id, admin, "admin")
        with self.assertRaises(Forbidden):
            party.set_role(admin, member, "admin")
        party.set_role(admin, member, "guest")
        party.set_role(admin, member, "member")

    def test_an_ordinary_member_cannot_change_roles_and_the_owner_role_is_fixed(self):
        party = self.party()
        alex, _ = self.member(party, "Alex")
        sam, _ = self.member(party, "Sam")
        with self.assertRaises(Forbidden):
            party.set_role(alex, sam, "guest")
        with self.assertRaises(Forbidden):
            party.set_role(party.host_id, party.host_id, "member")
        with self.assertRaises(NotFound):
            party.set_role(party.host_id, sam, "owner")

    def test_a_guest_reads_the_chat_but_cannot_post_share_or_join_voice(self):
        party = self.party()
        guest, session = self.member(party, "Gus", role="guest")
        with self.assertRaises(Forbidden):
            party.chat(guest, "hello")
        self.assertEqual(self.post(party, "/chat", {"text": "hello"}, session)[0], 403)
        self.assertFalse(party.set_share(guest, 0, True, "Screen"))
        with self.assertRaises(Forbidden):
            party.join_voice(guest, "voice-1")
        history = read_until(self.stream(party, session), "history")
        self.assertEqual(history["messages"], [])

    def test_becoming_a_guest_drops_voice_and_shares_at_once(self):
        party = self.party()
        alex, session = self.member(party, "Alex", user="u-alex")
        party.join_voice(alex, "voice-1")
        party.set_share(alex, 0, True, "Screen")
        party.set_role(party.host_id, alex, "guest")
        entry = next(m for m in party.roster() if m["id"] == alex)
        self.assertEqual((entry["role"], entry["voice"], entry["sharing"]), ("guest", None, False))

    def test_a_role_change_reaches_the_person_and_the_roster(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        stream = self.stream(party, session)
        read_until(stream, "hello")
        party.set_role(party.host_id, alex, "guest")
        self.assertEqual(read_until(stream, "role")["role"], "guest")

    def test_role_routes(self):
        party = self.party()
        alex, alex_session = self.member(party, "Alex")
        sam, _ = self.member(party, "Sam")
        status, _ = self.post(party, "/role", {"id": sam, "role": "guest"}, alex_session)
        self.assertEqual(status, 403)
        owner_session = party.host_token
        status, _ = self.post(party, "/role", {"id": sam, "role": "guest"}, owner_session, cookie="privatepartynet_host")
        self.assertEqual(status, 202)


class BanTests(ServerTestCase):
    def test_a_banned_person_cannot_join_again_even_after_a_restart(self):
        first = self.party()
        alex, _ = self.member(first, "Alex", user="u-alex")
        self.assertTrue(first.ban(first.host_id, alex))
        self.assertIsNone(first.redeem(first.join_token("Alex", user="u-alex")))
        first.stop()
        second = self.party()
        self.assertIsNone(second.redeem(second.join_token("Alex", user="u-alex")))
        self.assertEqual(second.banned(), ["u-alex"])

    def test_unbanning_lets_them_back(self):
        party = self.party()
        alex, _ = self.member(party, "Alex", user="u-alex")
        party.ban(party.host_id, alex)
        self.assertTrue(party.unban(party.host_id, "u-alex"))
        self.assertIsNotNone(party.redeem(party.join_token("Alex", user="u-alex")))

    def test_a_person_without_an_id_cannot_be_banned_and_members_cannot_ban(self):
        party = self.party()
        guest, _ = self.member(party, "Anon")
        alex, _ = self.member(party, "Alex", user="u-alex")
        self.assertFalse(party.ban(party.host_id, guest))
        self.assertFalse(party.ban(guest, alex))

    def test_coming_back_replaces_the_old_offline_entry(self):
        party = self.party()
        self.member(party, "Alex", user="u-alex")
        before = len(party.roster())
        self.member(party, "Alex", user="u-alex")
        self.assertEqual(len(party.roster()), before, "the old, offline Alex is replaced, not kept beside the new one")


class JoinRequestTests(ServerTestCase):
    def test_the_owner_and_admins_are_told_and_either_can_decide(self):
        party = self.party()
        admin, admin_session = self.member(party, "Ada", user="u-ada")
        party.set_role(party.host_id, admin, "admin")
        admin_stream = self.stream(party, admin_session)
        read_until(admin_stream, "hello")
        request, _ = party.request_join("Newcomer", user="u-new", address="203.0.113.5")
        seen = read_until(admin_stream, "join_request")
        self.assertEqual((seen["id"], seen["name"], seen["user"]), (request, "Newcomer", "u-new"))
        token = party.decide_request(admin, request, True)
        self.assertIsNotNone(party.redeem(token))
        self.assertEqual(read_until(admin_stream, "join_resolved")["allowed"], True)

    def test_a_denied_request_gives_no_token_and_is_gone(self):
        party = self.party()
        request, _ = party.request_join("Newcomer")
        self.assertIsNone(party.decide_request(party.host_id, request, False))
        with self.assertRaises(NotFound):
            party.decide_request(party.host_id, request, True)

    def test_an_ordinary_member_cannot_decide(self):
        party = self.party()
        alex, _ = self.member(party, "Alex")
        request, _ = party.request_join("Newcomer")
        with self.assertRaises(Forbidden):
            party.decide_request(alex, request, True)
        self.assertEqual(len(party.pending_requests()), 1)

    def test_an_admin_who_connects_later_sees_the_waiting_requests(self):
        party = self.party()
        party.request_join("Waiting")
        admin, session = self.member(party, "Ada", user="u-ada")
        party.set_role(party.host_id, admin, "admin")
        stream = self.stream(party, session)
        self.assertEqual(read_until(stream, "join_request")["name"], "Waiting")

    def test_a_banned_person_cannot_even_ask(self):
        party = self.party()
        alex, _ = self.member(party, "Alex", user="u-alex")
        party.ban(party.host_id, alex)
        with self.assertRaises(Forbidden):
            party.request_join("Alex", user="u-alex")

    def test_the_decision_route(self):
        party = self.party()
        alex, alex_session = self.member(party, "Alex")
        request, _ = party.request_join("Newcomer")
        self.assertEqual(self.post(party, "/decide", {"id": request, "allow": True}, alex_session)[0], 403)
        request, ticket = party.request_join("Second")
        status, data = self.post(party, "/decide", {"id": request, "allow": True}, party.host_token, cookie="privatepartynet_host")
        self.assertEqual(status, 200)
        self.assertNotIn(b"token", data, "the join token goes to the joiner only, never to the page that decided")
        answer = party.answer(ticket)
        self.assertEqual(answer["state"], "allowed")
        self.assertIsNotNone(party.redeem(answer["token"]))

    def test_a_request_expires(self):
        party = self.party()
        request, _ = party.request_join("Slow")
        party._requests[request]["until"] = time.monotonic() - 1
        self.assertEqual(party.pending_requests(), [])


class FrontDoorTests(ServerTestCase):
    """Someone outside sends a name and the password over HTTPS, an admin decides, and they pick up a join token."""

    def signed_ask(self, party, identity, name, password="", fingerprint=None, nonce=None, tamper=None):
        from privatepartynet.server import ask_message

        response = self.open(party, "GET", "/ask?challenge=1")
        challenge = json.loads(response.read())
        nonce = nonce or challenge["nonce"]
        signature = identity.sign(ask_message(fingerprint or party.fingerprint, nonce, name)).hex()
        body = {"name": name, "password": password, "card": identity.card(name), "nonce": nonce, "sig": signature}
        if tamper:
            tamper(body)
        response = self.open(party, "POST", "/ask", json.dumps(body), JSON_HEADERS)
        return response.status, response.read()

    def ask(self, party, name="Newcomer", password="428617", user=None):
        body = {"name": name, "password": password}
        if user:
            body["user"] = user
        response = self.open(party, "POST", "/ask", json.dumps(body), JSON_HEADERS)
        return response.status, response.read()

    def status(self, party, ticket):
        response = self.open(party, "GET", f"/ask?ticket={ticket}")
        return json.loads(response.read())

    def test_a_wrong_password_is_refused_and_nothing_is_queued(self):
        party = self.party()
        party.passcode = "428617"
        status, _ = self.ask(party, password="000000")
        self.assertEqual(status, 403)
        self.assertEqual(party.pending_requests(), [])

    def test_the_right_password_waits_for_a_decision_then_hands_over_the_token(self):
        party = self.party()
        party.passcode = "428617"
        status, data = self.ask(party, user="u-new")
        self.assertEqual(status, 200)
        ticket = json.loads(data)["ticket"]
        self.assertEqual(self.status(party, ticket), {"state": "pending"})
        (waiting,) = party.pending_requests()
        token = party.decide_request(party.host_id, waiting["id"], True)
        answer = self.status(party, ticket)
        self.assertEqual((answer["state"], answer["token"]), ("allowed", token))
        self.assertEqual(self.status(party, ticket), {"state": "gone"}, "the token is handed over once")
        self.assertIsNotNone(party.redeem(token))

    def test_a_denied_person_is_told_so(self):
        party = self.party()
        party.passcode = "428617"
        ticket = json.loads(self.ask(party)[1])["ticket"]
        (waiting,) = party.pending_requests()
        party.decide_request(party.host_id, waiting["id"], False)
        self.assertEqual(self.status(party, ticket)["state"], "denied")

    def test_an_unknown_ticket_is_gone_not_an_error(self):
        party = self.party()
        self.assertEqual(self.status(party, "nope"), {"state": "gone"})

    def test_a_bad_request_is_a_400(self):
        party = self.party()
        response = self.open(party, "POST", "/ask", json.dumps({"name": 5}), JSON_HEADERS)
        self.assertEqual(response.status, 400)
        response.read()

    def test_one_address_cannot_fill_the_waiting_list(self):
        party = self.party()
        party.passcode = "428617"
        results = [self.ask(party, name=f"N{i}")[0] for i in range(5)]
        self.assertEqual(results, [200, 200, 200, 409, 409])

    def test_a_banned_person_is_turned_away_at_the_door(self):
        from privatepartynet.identity import Identity

        party = self.party()
        party.passcode = "428617"
        alex = Identity.create()
        member, _ = self.member(party, "Alex", user=alex.user_id)
        party.ban(party.host_id, member)
        status, body = self.signed_ask(party, alex, "Alex")
        self.assertEqual(status, 403)
        self.assertIn(b"banned", body)

    def test_the_owner_sees_the_request_arrive_in_the_open_window(self):
        party = self.party()
        party.passcode = "428617"
        stream = self.stream(party, party.host_token, cookie="privatepartynet_host")
        read_until(stream, "hello")
        self.ask(party, name="Dana")
        self.assertEqual(read_until(stream, "join_request")["name"], "Dana")


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(64))
GIF = b"GIF89a" + bytes(range(64))
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(range(32))


class ImageTests(ServerTestCase):
    def upload(self, party, session, data, cookie="privatepartynet_member"):
        response = self.open(party, "POST", "/blob", data, {"Content-Type": "application/octet-stream", "Cookie": f"{cookie}={session}"})
        return response.status, response.read()

    def test_each_accepted_type_is_stored_and_named_by_its_hash(self):
        import hashlib
        party = self.party()
        _, session = self.member(party, "Alex")
        for data, mime in ((PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")):
            status, body = self.upload(party, session, data)
            self.assertEqual(status, 200, mime)
            info = json.loads(body)
            self.assertEqual((info["hash"], info["mime"], info["size"]), (hashlib.sha256(data).hexdigest(), mime, len(data)))

    def test_what_is_not_a_picture_is_refused_whatever_it_claims_to_be(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        self.assertEqual(self.upload(party, session, b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")[0], 415)
        self.assertEqual(self.upload(party, session, b"MZ" + bytes(100))[0], 415)
        self.assertEqual(self.upload(party, session, b"")[0], 413)

    def test_a_picture_over_two_megabytes_is_refused(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        try:
            status = self.upload(party, session, PNG + bytes(2 * 1024 * 1024))[0]
        except (ssl.SSLError, ConnectionError):
            status = 413  # the server answered and hung up before the whole upload was sent; that is the refusal
        self.assertEqual(status, 413)

    def test_a_guest_cannot_upload_and_a_stranger_cannot_either(self):
        party = self.party()
        _, guest = self.member(party, "Gus", role="guest")
        self.assertEqual(self.upload(party, guest, PNG)[0], 403)
        self.assertEqual(self.upload(party, "not-a-session", PNG)[0], 401)

    def test_uploads_are_limited_per_minute(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        results = [self.upload(party, session, PNG + bytes([i]))[0] for i in range(12)]
        self.assertEqual(results[:10], [200] * 10)
        self.assertEqual(results[10:], [403, 403])

    def test_a_message_can_carry_a_picture_and_it_is_kept(self):
        party = self.party()
        alex, session = self.member(party, "Alex", user="u-alex")
        info = json.loads(self.upload(party, session, PNG)[1])
        message = party.chat(alex, "look", "general", {"hash": info["hash"], "w": 800, "h": 600})
        self.assertEqual(message["image"], {"hash": info["hash"], "w": 800, "h": 600})
        (saved,) = party.channel_history(alex, "general")
        self.assertEqual(saved["image"]["hash"], info["hash"])
        self.assertEqual(self.store.get_blob(info["hash"]), PNG)

    def test_a_picture_that_was_never_uploaded_cannot_be_attached(self):
        party = self.party()
        alex, _ = self.member(party, "Alex")
        with self.assertRaises(NotFound):
            party.chat(alex, "", "general", {"hash": "0" * 64, "w": 1, "h": 1})

    def test_a_picture_alone_is_a_message_and_odd_sizes_are_zeroed(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        info = json.loads(self.upload(party, session, PNG)[1])
        status, _ = self.post(party, "/chat", {"channel": "general", "image": {"hash": info["hash"], "w": -5, "h": "wide"}}, session)
        self.assertEqual(status, 202)
        (saved,) = party.channel_history(alex, "general")
        self.assertEqual((saved["text"], saved["image"]["w"], saved["image"]["h"]), ("", 0, 0))

    def test_members_fetch_pictures_with_the_right_headers_and_strangers_do_not(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        info = json.loads(self.upload(party, session, JPEG)[1])
        response = self.open(party, "GET", f"/blob?hash={info['hash']}", headers={"Cookie": f"privatepartynet_member={session}"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("sandbox", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.read(), JPEG)
        response = self.open(party, "GET", f"/blob?hash={info['hash']}")
        self.assertEqual(response.status, 401)
        response.read()
        response = self.open(party, "GET", "/blob?hash=../../etc", headers={"Cookie": f"privatepartynet_member={session}"})
        self.assertEqual(response.status, 404)
        response.read()

    def test_pictures_work_without_a_store_too(self):
        party = self.party(store=None)
        alex, session = self.member(party, "Alex")
        info = json.loads(self.upload(party, session, GIF)[1])
        self.assertEqual(party.get_image(alex, info["hash"]), ("image/gif", GIF))


class ReviewFrontDoorTests(ServerTestCase):
    """What the review of November 1st found and fixed at the front door. Each test tries to get in the wrong way."""

    ask = FrontDoorTests.ask
    status = FrontDoorTests.status
    signed_ask = FrontDoorTests.signed_ask

    def setUp(self):
        super().setUp()
        from privatepartynet.identity import Identity

        self.Identity = Identity

    def test_a_claimed_id_without_the_keys_gives_nothing(self):
        party = self.party()
        party.passcode = "428617"
        admin = self.Identity.create()
        member, _ = self.member(party, "Ada", user=admin.user_id)
        party.set_role(party.host_id, member, "admin")
        status, data = self.ask(party, name="Ada", user=admin.user_id)  # an unsigned claim to Ada's id
        self.assertEqual(status, 200)
        (waiting,) = party.pending_requests()
        self.assertIsNone(waiting["user"], "an unproven id is dropped, so it cannot inherit a role")

    def test_a_returning_member_who_proves_it_walks_straight_back_in_with_their_role(self):
        party = self.party()
        party.passcode = "428617"
        ada = self.Identity.create()
        member, _ = self.member(party, "Ada", user=ada.user_id)
        party.set_role(party.host_id, member, "admin")
        status, body = self.signed_ask(party, ada, "Ada")  # no password at all
        self.assertEqual(status, 200)
        self.assertEqual(party.pending_requests(), [], "nobody had to say yes")
        answer = self.status(party, json.loads(body)["ticket"])
        self.assertEqual(answer["state"], "allowed")
        session = party.redeem(answer["token"])
        self.assertEqual(party.role_for(session), "admin")

    def test_a_new_person_who_signs_still_needs_the_password_and_a_yes(self):
        party = self.party()
        party.passcode = "428617"
        newcomer = self.Identity.create()
        self.assertEqual(self.signed_ask(party, newcomer, "New")[0], 403)
        status, body = self.signed_ask(party, newcomer, "New", password="428617")
        self.assertEqual(status, 200)
        (waiting,) = party.pending_requests()
        self.assertEqual(waiting["user"], newcomer.user_id, "the proven id is kept, so a role given later sticks")

    def test_someone_elses_signature_does_not_open_the_door(self):
        party = self.party()
        ada, mallory = self.Identity.create(), self.Identity.create()
        self.member(party, "Ada", user=ada.user_id)

        def as_ada(body):
            body["card"] = ada.card("Ada")  # Ada's card, Mallory's signature

        status, body = self.signed_ask(party, mallory, "Ada", tamper=as_ada)
        self.assertEqual(status, 403)
        self.assertIn(b"identity", body)

    def test_a_card_whose_id_is_not_its_keys_is_refused(self):
        party = self.party()
        ada = self.Identity.create()

        def other_id(body):
            body["card"]["id"] = self.Identity.create().user_id

        self.assertEqual(self.signed_ask(party, ada, "Ada", tamper=other_id)[0], 403)

    def test_a_challenge_works_once(self):
        party = self.party()
        party.passcode = "428617"
        ada = self.Identity.create()
        self.member(party, "Ada", user=ada.user_id)
        nonce = party.challenge()
        self.assertEqual(self.signed_ask(party, ada, "Ada", nonce=nonce)[0], 200)
        status, body = self.signed_ask(party, ada, "Ada", nonce=nonce)
        self.assertEqual(status, 403)
        self.assertIn(b"expired", body)

    def test_a_made_up_challenge_is_refused(self):
        party = self.party()
        ada = self.Identity.create()
        self.member(party, "Ada", user=ada.user_id)
        self.assertEqual(self.signed_ask(party, ada, "Ada", nonce="00" * 16)[0], 403)

    def test_a_signature_made_for_another_host_is_refused(self):
        """A fake host that passes the real host's challenge along gets a signature over its own certificate, which the
        real host refuses."""
        party = self.party()
        ada = self.Identity.create()
        self.member(party, "Ada", user=ada.user_id)
        self.assertEqual(self.signed_ask(party, ada, "Ada", fingerprint="ab" * 32)[0], 403)

    def test_the_signature_covers_the_name(self):
        party = self.party()
        ada = self.Identity.create()
        self.member(party, "Ada", user=ada.user_id)

        def rename(body):
            body["name"] = "Somebody Else"

        self.assertEqual(self.signed_ask(party, ada, "Ada", tamper=rename)[0], 403)

    def test_a_removed_member_has_to_ask_again_and_loses_their_role(self):
        party = self.party()
        party.passcode = "428617"
        ada = self.Identity.create()
        member, _ = self.member(party, "Ada", user=ada.user_id)
        party.set_role(party.host_id, member, "admin")
        self.assertTrue(party.kick(party.host_id, member))
        status, body = self.signed_ask(party, ada, "Ada", password="428617")
        self.assertEqual(status, 200)
        self.assertEqual(len(party.pending_requests()), 1, "after a removal, a yes is needed again")
        token = party.decide_request(party.host_id, party.pending_requests()[0]["id"], True)
        self.assertEqual(party.role_for(party.redeem(token)), "member")

    def test_wrong_passwords_shut_one_address_out(self):
        party = self.party()
        party.passcode = "428617"
        results = [self.ask(party, password="000000")[0] for _ in range(5)]
        self.assertEqual(results, [403] * 5)
        status, body = self.ask(party, password="428617")
        self.assertEqual(status, 409, "even the right password waits once an address has guessed five times")
        self.assertIn(b"wrong passwords", body)

    def test_many_addresses_guessing_together_close_the_door_for_a_minute(self):
        from unittest import mock
        from privatepartynet import server as server_module

        party = self.party()
        party.passcode = "428617"
        with mock.patch.object(server_module, "ASK_FAILS_PER_MINUTE", 4):
            for number in range(4):
                with self.assertRaises(Forbidden):
                    party.ask("x", "wrong", address=f"198.51.100.{number}")
            from privatepartynet.server import Full
            with self.assertRaises(Full):
                party.ask("x", "428617", address="198.51.100.99")

    def test_on_this_network_no_password_is_needed_but_a_yes_still_is(self):
        party = self.party(trust_local=True)
        status, _ = self.ask(party, password="")
        self.assertEqual(status, 200)
        self.assertEqual(len(party.pending_requests()), 1)
        with self.assertRaises(Forbidden):
            party.ask("Far", "", address="203.0.113.9")  # from outside, the password is needed
        with self.assertRaises(Forbidden):
            party.ask("Shared", "", address="100.64.1.2")  # a carrier-grade NAT address is not this network

    def test_the_routers_own_address_never_counts_as_this_network(self):
        party = self.party(trust_local=True)
        party.ask("Neighbour", "", address="192.168.0.20")
        party.untrusted.add("192.168.0.1")
        with self.assertRaises(Forbidden):
            party.ask("Through the router", "", address="192.168.0.1")

    def test_what_counts_as_this_network(self):
        from privatepartynet.server import is_local_address

        for address in ("192.168.0.5", "10.1.2.3", "172.20.0.1", "127.0.0.1", "169.254.3.4", "::1", "fe80::1%12",
                        "fd12::5", "::ffff:192.168.1.2"):
            self.assertTrue(is_local_address(address), address)
        for address in ("127.1.5.9", "9.9.9.9", "100.64.1.2", "100.127.0.9", "203.0.113.9", "198.51.100.1", "198.18.0.1",
                        "26.10.20.30", "2603:6011::1", "::ffff:8.8.8.8", "", "nonsense", "172.32.0.1"):
            self.assertFalse(is_local_address(address), address)

    def test_the_password_is_kept_with_the_server_and_only_the_owner_changes_it(self):
        first = self.party()
        self.assertEqual(first.passcode, "428617")
        alex, _ = self.member(first, "Alex")
        with self.assertRaises(Forbidden):
            first.set_password(alex, "new password")
        with self.assertRaises(NotFound):
            first.set_password(first.host_id, "short")
        first.set_password(first.host_id, "river-stone-42")
        first.stop()
        second = self.party()  # started with "428617" again, as the app does
        self.assertEqual(second.passcode, "river-stone-42", "a restart keeps the server's own password")


class RemoteMediaTests(ServerTestCase):
    def test_someone_from_outside_turns_stun_on_for_every_page(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        stream = self.stream(party, session)
        self.assertFalse(read_until(stream, "hello")["stun"])
        self.assertFalse(party.note_join_address("192.168.0.9"), "a neighbour changes nothing")
        self.assertTrue(party.note_join_address("9.9.9.9"))
        self.assertTrue(read_until(stream, "ice")["stun"])
        self.assertFalse(party.note_join_address("203.0.113.50"), "once is enough")
        _, later = self.member(party, "Late")
        self.assertTrue(read_until(self.stream(party, later), "hello")["stun"])

    def test_a_join_from_this_pc_does_not(self):
        party = self.party()
        token = party.join_token("Alex")
        response = self.open(party, "GET", f"/join?token={token}")
        self.assertEqual(response.status, 303)
        response.read()
        self.assertFalse(party.remote)


class InputLimitTests(ServerTestCase):
    def raw(self, party, request_bytes, wait=5):
        import socket as socket_module
        import ssl as ssl_module

        context = insecure_context()
        sock = socket_module.create_connection(("127.0.0.1", party.port), timeout=wait)
        tls = context.wrap_socket(sock, server_hostname="localhost")
        self.addCleanup(tls.close)
        tls.sendall(request_bytes)
        data = b""
        try:
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    break
        except (socket_module.timeout, ssl_module.SSLError, ConnectionError):
            pass
        return data

    def test_a_negative_length_is_answered_at_once(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        started = time.monotonic()
        reply = self.raw(party, (f"POST /chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                                 f"Cookie: privatepartynet_member={session}\r\nContent-Length: -1\r\n\r\n").encode())
        self.assertLess(time.monotonic() - started, 3, "the server did not wait for the connection to close")
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0] + b" ")
        reply = self.raw(party, (f"POST /blob HTTP/1.1\r\nHost: x\r\nCookie: privatepartynet_member={session}\r\n"
                                 f"Content-Length: -5\r\n\r\n").encode())
        self.assertIn(b" 413 ", reply.split(b"\r\n")[0] + b" ")

    def test_connections_beyond_the_limit_are_closed_not_queued(self):
        import socket as socket_module
        from unittest import mock
        from privatepartynet import server as server_module

        with mock.patch.object(server_module, "MAX_CONNECTIONS", 2):
            party = self.party()
        idle = []
        for _ in range(2):  # two connections that never say anything hold both slots
            idle.append(socket_module.create_connection(("127.0.0.1", party.port), timeout=5))
        self.addCleanup(lambda: [s.close() for s in idle])
        time.sleep(0.3)
        extra = socket_module.create_connection(("127.0.0.1", party.port), timeout=5)
        self.addCleanup(extra.close)
        started = time.monotonic()
        try:
            got = extra.recv(1)
        except ConnectionError:
            got = b""
        self.assertEqual(got, b"", "the extra connection was closed")
        self.assertLess(time.monotonic() - started, 3)
        for s in idle:
            s.close()
        time.sleep(0.5)
        response = self.open(party, "GET", "/ask?ticket=x")
        self.assertEqual(response.status, 200, "slots come back when connections end")
        response.read()


class VoicePresenceTests(ServerTestCase):
    def test_joining_a_voice_channel_shows_in_the_roster_and_leaving_clears_it(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        self.assertEqual(self.post(party, "/voice", {"channel": "voice-1"}, session)[0], 202)
        entry = next(m for m in party.roster() if m["id"] == alex)
        self.assertEqual(entry["voice"], "voice-1")
        self.assertEqual(self.post(party, "/voice", {"channel": None}, session)[0], 202)
        self.assertIsNone(next(m for m in party.roster() if m["id"] == alex)["voice"])

    def test_a_text_channel_is_not_a_voice_channel(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        self.assertEqual(self.post(party, "/voice", {"channel": "general"}, session)[0], 404)

    def test_a_voice_channel_holds_six_people(self):
        party = self.party()
        for number in range(MAX_VOICE):
            person, _ = self.member(party, f"P{number}")
            party.join_voice(person, "voice-1")
        extra, session = self.member(party, "Late")
        self.assertEqual(self.post(party, "/voice", {"channel": "voice-1"}, session)[0], 409)
        party.join_voice(next(m["id"] for m in party.roster() if m["name"] == "P0"), None)
        self.assertEqual(self.post(party, "/voice", {"channel": "voice-1"}, session)[0], 202)

    def test_disconnecting_leaves_the_voice_channel(self):
        party = self.party()
        alex, session = self.member(party, "Alex")
        stream = self.stream(party, session)
        read_until(stream, "hello")
        party.join_voice(alex, "voice-1")
        stream.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if next(m for m in party.roster() if m["id"] == alex)["voice"] is None:
                break
            time.sleep(0.1)
        self.assertIsNone(next(m for m in party.roster() if m["id"] == alex)["voice"])

    def test_removing_the_voice_channel_empties_it(self):
        party = self.party()
        alex, _ = self.member(party, "Alex")
        party.join_voice(alex, "voice-1")
        party.remove_channel(party.host_id, "voice-1")
        self.assertIsNone(next(m for m in party.roster() if m["id"] == alex)["voice"])


if __name__ == "__main__":
    unittest.main()
