"""Mixed versions (3.3): people update at different times, so a 3.3 host meets members whose app is 3.2 or older, and a
3.3 member meets hosts that are. A joining app says what it can do ("caps" in POST /ask); one that says nothing gets
3.2's behaviour from a 3.3 host: no heartbeat (its link reads ok with no round trip), no grace and no resume, offline the
moment its connection closes, and back by its cookie or by asking again. A 3.3 app asking a 3.2 host gets no resume
token and nothing new, and its party window still works through its link."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from privatepartynet import connect
from privatepartynet.identity import Identity
from privatepartynet.server import ask_message
from test_resilience_server import Base
from test_server import next_event, read_until

ROOT = Path(__file__).resolve().parents[1]
# The versions people may still run, each as it was released: 3.2.0 (signed updates), and K's 3.3.0 and 3.4.0 (the one
# window), none of which says "caps" when it asks to join or knows of a heartbeat.
OLD_HOSTS = (("3.2.0", "e87c441"), ("3.3.0", "a21cffb"), ("3.4.0", "9e6bbd1"))


class NewHostOldAppTests(Base):
    """A 3.3 host, and a member whose app is 3.2: its POST /ask said nothing about caps."""

    def old_member(self, party, name="Old"):
        identity = Identity.create()
        token = party.join_token(name, user=identity.user_id)  # no caps: what a 3.2 app's join comes to
        self.assertNotIn(token, party._join_resume, "no resume token for an app that cannot use one")
        session = party.redeem(token)
        return party.member_for(session), session, identity

    def test_no_heartbeat_and_the_link_reads_ok_while_the_connection_is_open(self):
        party = self.party()
        old, session, _ = self.old_member(party)
        stream = self.stream(party, session)
        read_until(stream, "hello")
        party.pulse()
        links = read_until(stream, "links")
        self.assertEqual(links["links"][old], {"state": "ok", "rtt": None, "loss": None, "since": int(party._started)})
        self.time.tick(300)
        party.pulse()
        entry = self.entry(party, old)
        self.assertEqual((entry["online"], entry["link"]["state"]), (True, "ok"),
                         "never taken out for not answering a heartbeat it was never sent")
        party.pulse()  # a pulse sends the links; a ping to this member would come before them
        while True:
            event = next_event(stream)
            self.assertNotEqual(event.get("type"), "ping")
            if event.get("type") == "links":
                break

    def test_a_dropout_is_3_2s_offline_at_once_and_the_cookie_or_a_new_ask_brings_them_back(self):
        party = self.party()
        old, session, identity = self.old_member(party)
        party.join_voice(old, "voice-1")
        stream = self.stream(party, session)
        read_until(stream, "hello")
        stream.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.entry(party, old)["online"]:
            time.sleep(0.05)
        entry = self.entry(party, old)
        self.assertEqual((entry["online"], entry["voice"]), (False, None), "as in 3.2: offline, out of voice")
        self.time.tick(15)
        party.pulse()
        self.assertEqual(self.get(party, "/link", session)[0], 200, "never suspended: no 409 to get stuck on")
        read_until(self.stream(party, session), "hello")  # the page's own reconnect, with its cookie
        self.assertTrue(self.entry(party, old)["online"])
        # The app was restarted instead: it asks again the normal way and, known, comes straight in.
        nonce = party.challenge()
        ticket = party.ask("Old", "", "203.0.113.5", identity.card("Old"), nonce,
                           identity.sign(ask_message(party.fingerprint, nonce, "Old")).hex())
        answer = party.answer(ticket)
        self.assertEqual(answer["state"], "allowed")
        self.assertNotIn("resume", answer)

    def test_a_new_app_and_an_old_one_in_the_same_party(self):
        party = self.party()
        old, old_session, _ = self.old_member(party)
        new, new_session, _, _ = self.joined(party, "New")
        for session in (old_session, new_session):
            read_until(self.stream(party, session), "hello")
        party.pulse()
        self.time.tick(11)
        party.pulse()
        self.assertEqual(self.entry(party, new)["link"]["state"], "reconnecting", "the new app's page did not answer")
        self.assertEqual(self.entry(party, old)["link"]["state"], "ok", "the old one is not measured at all")


def _old_module(ref, name):
    """privatepartynet/<name>.py as it was at commit `ref`, loaded beside today's modules, or None without git."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        source = subprocess.run([git, "show", f"{ref}:privatepartynet/{name}.py"], cwd=ROOT, capture_output=True,
                                check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    folder = Path(tempfile.mkdtemp())
    path = folder / f"{name}_{ref}.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location(f"privatepartynet.{name}_{ref}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OLD_SERVERS = {version: _old_module(ref, "server") for version, ref in OLD_HOSTS}
OLD_CONNECTS = {version: _old_module(ref, "connect") for version, ref in OLD_HOSTS[1:]}


class OldAppNewHostTests(Base):
    """K's 3.3.0 and 3.4.0 apps (their own Joiner, which says nothing about caps) asking a 3.5 host."""

    def test_an_older_apps_join_gets_no_resume_token_and_no_heartbeat(self):
        party = self.party(trust_local=True)
        for version, old in OLD_CONNECTS.items():
            if old is None:
                self.skipTest("git and the old commits are needed")
            with self.subTest(version):
                identity = Identity.create()
                invite = {"name": "New host", "password": "", "candidates": [["lan", "127.0.0.1", party.port]],
                          "fingerprint": party.fingerprint, "server_id": "", "host_sign": ""}
                joiner = old.Joiner(invite, "Old " + version, identity=identity, my_lan=["127.0.0.1"], poll=0.1,
                                    timeout=3.0)
                thread = threading.Thread(target=joiner.run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not party.pending_requests():
                    time.sleep(0.1)
                (request,) = party.pending_requests()
                party.decide_request(party.host_id, request["id"], True)
                thread.join(15)
                self.assertEqual(joiner.status["state"], "allowed", joiner.status)
                self.assertFalse(party._join_resume, "no resume token for an app that cannot use one")
                session = party.redeem(joiner.status["url"].split("token=")[1].split("&")[0])
                member = party.member_for(session)
                self.assertIsNone(party._members[member]["meter"], "no heartbeat: 3.2's ways")
                self.assertEqual(self.entry(party, member)["link"]["rtt"], None)


class OldHostNewAppTests(unittest.TestCase):
    """A host on 3.2.0, K's 3.3.0 or K's 3.4.0, and a member whose app is this one."""

    def test_a_new_app_joins_each_older_host_and_its_room_works_through_its_link(self):
        for version, module in OLD_SERVERS.items():
            if module is None:
                self.skipTest("git and the old commits are needed to run the old hosts")
            with self.subTest(version):
                self.old_server = module
                self.setUp_host()
                try:
                    self.join_old_host()
                finally:
                    self.cleanup_host()

    def setUp_host(self):
        from privatepartynet.app import Controller
        from privatepartynet.cert import ensure_certificate
        from test_app import FakeGrabber

        self.temp = tempfile.TemporaryDirectory()
        certfile, keyfile = ensure_certificate(self.temp.name, ["localhost", "127.0.0.1"])
        self.party = self.old_server.Party("428617", 0, certfile, keyfile, party_name="Old host", host_name="Host",
                                           failed_login_delay=0, trust_local=False, heartbeat=0.2)
        self.party.start()
        self.guest = Controller(data_folder=self.temp.name + "/guest", discovery_port=0, grabber=FakeGrabber())
        self.guest._name = "Guest"
        self.links = []

    def cleanup_host(self):
        for link in self.links:
            link.close()
        self.guest.shutdown()
        self.party.stop()
        self.temp.cleanup()

    def join_old_host(self):
        invite = {"name": "Old host", "password": "428617", "candidates": [["lan", "127.0.0.1", self.party.port]],
                  "fingerprint": self.party.fingerprint, "server_id": "", "host_sign": ""}
        joiner = connect.Joiner(invite, "Guest", identity=self.guest._identity, my_lan=[], poll=0.1, timeout=3.0)
        thread = threading.Thread(target=joiner.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.party.pending_requests():
            time.sleep(0.1)
        (request,) = self.party.pending_requests()  # the "caps" it sent were ignored, and nothing broke
        self.party.decide_request(self.party.host_id, request["id"], True)
        thread.join(15)
        self.assertEqual(joiner.status["state"], "allowed", joiner.status)
        self.assertEqual(joiner.resume_token, "", "an old host hands out no resume token")
        data = {"url": joiner.status["url"], "name": "Old host",
                "link": {"way": joiner.way, "invite": dict(invite), "resume_token": joiner.resume_token, "key": ""}}
        entry = self.guest._open_link(data, [self.party.fingerprint])
        self.links.append(entry["link"])
        token = joiner.status["url"].split("token=")[1]
        connection = connect._connect(entry["link"].address, entry["link"].port, [self.party.fingerprint], 5.0)
        connection.request("GET", f"/join?token={token}")
        response = connection.getresponse()
        response.read()
        cookie = response.getheader("Set-Cookie").split(";")[0]  # (K's hosts name it per server)
        connection.close()
        self.assertEqual(response.status, 303)
        connection = connect._connect(entry["link"].address, entry["link"].port, [self.party.fingerprint], 5.0)
        connection.request("GET", "/events", headers={"Cookie": cookie})
        stream = connection.getresponse()
        self.assertEqual(stream.status, 200)
        seen = [read_until(stream, "hello")["type"]]
        ends = time.monotonic() + 3  # a 3.3 host would ping within 2 s; the old one's keep-alives keep lines coming
        while time.monotonic() < ends:
            line = stream.readline()
            if line.startswith(b"data: "):
                seen.append(json.loads(line[6:]).get("type"))
        connection.close()
        self.assertNotIn("ping", seen, "an old host sends no heartbeat: the ping display shows nothing known")
        result = entry["resume"]()
        self.assertFalse(result["ok"])
        self.assertIn("ask to join again", result["message"])


if __name__ == "__main__":
    unittest.main()
