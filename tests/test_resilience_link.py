"""Bad internet does not throw anyone out (3.3), from the member's side: a party window's link (partylink.py) between
a page and the host, and a stand-in for the page that does what room.js and link.js do (answer pings, reconnect with
growing pauses, resume through the app when the host asks for it, pick up missed chat). Between the link and the host
sits a TCP proxy that can lose time like a lossy link, go dark like an outage, or be replaced like a new address."""

import json
import logging
import random
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from privatepartynet import connect
from privatepartynet.cert import ensure_certificate
from privatepartynet.identity import Identity
from privatepartynet.partylink import LinkLost, PartyLink, backoff
from privatepartynet.server import Party, ask_message


class FlakyProxy:
    """127.0.0.1:port -> the party. `stall` is the chance that a chunk is held 0.2-1 s (what loss does to TCP); while
    `down`, nothing moves and new connections are dropped at once (the PartyLink's own check then says so)."""

    def __init__(self, target_port):
        self.target = ("127.0.0.1", target_port)
        self.down = False
        self.stall = 0.0
        self.closed = False
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._open = set()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            if self.down or self.closed:
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=5)
            except OSError:
                client.close()
                continue
            pair = (client, upstream)
            self._open.add(pair)
            ends = []
            threading.Thread(target=self._pipe, args=(client, upstream, pair, ends), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client, pair, ends), daemon=True).start()

    def _pipe(self, source, target, pair, ends):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                while self.down and not self.closed:
                    time.sleep(0.05)  # an outage: nothing moves, the connection hangs
                if self.stall and random.random() < self.stall:
                    time.sleep(random.uniform(0.2, 1.0))
                target.sendall(data)
        except OSError:
            pass
        finally:
            # As TCP does: one side finished sending, the other may still be answering. Close both once both are done.
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            ends.append(source)
            if len(ends) == 2:
                for sock in pair:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self._open.discard(pair)

    def cut(self):
        for pair in list(self._open):
            for sock in pair:
                try:
                    sock.close()
                except OSError:
                    pass

    def close(self):
        self.closed = True
        self.cut()
        self._sock.close()


page_log = logging.getLogger("test.page")


class Page:
    """What room.js and link.js do with the party connection, in Python, through a PartyLink."""

    def __init__(self, link, pins, session, resume=None, cookie="privatepartynet_member"):
        self.link, self.pins, self.session, self.resume = link, pins, session, resume
        self.cookie = cookie  # the host names its cookies per server (party.cookie_name)
        self.messages = {}  # id -> text
        self.last_seq = {}
        self.state = "ok"
        self.ended = None  # "gone" once the place has ended
        self.connects = 0
        self.resumes = 0
        self.heard = time.monotonic()
        self._stop = threading.Event()
        self._stream = None
        threading.Thread(target=self._run, daemon=True).start()

    def close(self):
        self._stop.set()
        self._drop()

    def _request(self, method, path, body=None, timeout=5.0):
        conn = connect._connect(self.link.address, self.link.port, self.pins, timeout)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Cookie": f"{self.cookie}={self.session}", "Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def _drop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass

    def _run(self):
        try:
            self._loop()
        except Exception:
            page_log.exception("the page's loop failed")
            raise

    def _loop(self):
        attempt = 0
        while not self._stop.is_set() and self.ended is None:
            if self.state == "reconnecting":
                status = 0
                try:
                    status, _ = self._request("GET", "/link")
                except (OSError, connect.ConnectError):
                    pass
                page_log.info("GET /link: %s", status)
                if status == 409 and self.resume is not None:
                    ok, gone, why = self.resume()
                    page_log.info("resume: ok=%s gone=%s %s", ok, gone, why)
                    self.resumes += ok
                    if gone:
                        self.ended = "gone"
                        return
                    status = 200 if ok else 0
                if status == 401:
                    self.ended = "gone"
                    return
                if status != 200:
                    self._stop.wait(backoff(attempt))
                    attempt += 1
                    continue
            attempt = 0
            self._listen()
            if self.ended is None and not self._stop.is_set():
                self.state = "reconnecting"

    def _listen(self):
        since = ",".join(f"{c}:{s}" for c, s in self.last_seq.items())
        try:
            conn = connect._connect(self.link.address, self.link.port, self.pins, 5.0)
            conn.request("GET", "/events" + (f"?since={since}" if since else ""),
                         headers={"Cookie": f"{self.cookie}={self.session}"})
            sock = conn.sock  # the response takes it over; the watchdog shuts it to end a stream that went quiet
            response = conn.getresponse()
            if response.status != 200:
                conn.close()
                return
        except (OSError, connect.ConnectError) as exc:
            page_log.info("events: %s", exc)
            return
        page_log.info("events open")
        sock.settimeout(None)
        self._stream = sock
        self.connects += 1
        self.heard = time.monotonic()
        threading.Thread(target=self._watch, args=(sock,), daemon=True).start()
        try:
            while not self._stop.is_set():
                line = response.fp.readline()
                if not line:
                    return
                if line.startswith(b"data: "):
                    self.heard = time.monotonic()
                    if self.state == "reconnecting":
                        self.state = "ok"
                    self._handle(json.loads(line[6:]))
        except (OSError, ValueError):
            return
        finally:
            self._drop()

    def _watch(self, conn):
        # link.js: nothing from the host for 10 s and the page stops waiting on this stream
        while self._stream is conn and not self._stop.is_set():
            if time.monotonic() - self.heard > 10:
                self._drop()
                return
            time.sleep(0.2)

    def _handle(self, message):
        kind = message.get("type")
        if kind == "ping":
            threading.Thread(target=self._pong, args=(message["id"],), daemon=True).start()
        elif kind == "history":
            for item in message["messages"]:
                self._note(item)
        elif kind == "chat":
            self._note(message)
        elif kind in ("dropped", "kicked"):
            page_log.info("ended: %s", kind)
            self.ended = "gone"

    def _pong(self, ping_id):
        try:
            self._request("POST", "/pong", {"id": ping_id}, timeout=4.0)
        except (OSError, connect.ConnectError):
            pass

    def _note(self, message):
        self.messages[message["id"]] = message["text"]
        channel = message["channel"]
        self.last_seq[channel] = max(self.last_seq.get(channel, 0), message.get("seq", 0))


def wait_for(condition, seconds):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class Base(unittest.TestCase):
    grace = 120.0

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.party = Party("river-stone-42", 0, certfile, keyfile, party_name="Heroes night", host_name="Host",
                           failed_login_delay=0, trust_local=False, reconnect_grace=self.grace)
        self.party.start()
        self.addCleanup(self.party.stop)
        self.pins = [self.party.fingerprint]
        self.proxy = self.make_proxy()
        self.ways = [self.proxy]
        self.identity = Identity.create()
        token = self.party.join_token("Alex", user=self.identity.user_id, caps=["link"])
        self.resume_token = self.party._join_resume[token]  # what the app gets with its join answer
        self.session = self.party.redeem(token)
        self.member = self.party.member_for(self.session)
        self.sam = self.party.member_for(self.party.redeem(self.party.join_token("Sam")))
        self.link = PartyLink(self.find, name="Heroes night", grace=self.grace)
        self.addCleanup(self.link.close)
        self.page = Page(self.link, self.pins, self.session, resume=self.resume,
                         cookie=self.party.cookie_name("privatepartynet_member"))
        self.addCleanup(self.page.close)
        self.assertTrue(wait_for(lambda: self.page.connects and self.state() == "ok", 10))

    def make_proxy(self):
        proxy = FlakyProxy(self.party.port)
        self.addCleanup(proxy.close)
        return proxy

    def find(self, force):
        """The app's ladder, cut down: the way in use while it answers, else the newest way that does."""
        way = self.ways[-1]
        if way.down or way.closed:
            raise OSError("the way to the host stopped answering")
        return ("127.0.0.1", way.port)

    def resume(self):
        return connect.resume(self.link.address, self.link.port, self.pins, self.resume_token, self.identity, "Alex")

    def state(self):
        entry = next((m for m in self.party.roster() if m["id"] == self.member), None)
        return entry["link"]["state"] if entry else None

    def present(self):
        return any(m["id"] == self.member for m in self.party.roster())


class LossTests(Base):
    def test_thirty_percent_loss_for_twenty_seconds_keeps_the_member_and_the_chat(self):
        self.proxy.stall = 0.3
        sent = []
        ends = time.monotonic() + 20
        while time.monotonic() < ends:
            text = f"while lossy {len(sent)}"
            self.party.chat(self.sam, text)
            sent.append(text)
            self.assertTrue(self.present(), "still in the party")
            time.sleep(2)
        self.proxy.stall = 0.0
        self.assertTrue(wait_for(lambda: set(sent) <= set(self.page.messages.values()), 20),
                        set(sent) - set(self.page.messages.values()))
        self.assertTrue(wait_for(lambda: self.state() == "ok", 15))
        self.assertEqual(self.party.pending_requests(), [], "nobody had to be let in again")


class OutageTests(Base):
    def test_a_fifteen_second_outage_resumes_without_a_new_approval_and_the_missed_chat_arrives(self):
        connects = self.page.connects
        self.proxy.down = True
        self.party.chat(self.sam, "said while Alex was away")
        self.assertTrue(wait_for(lambda: self.state() == "reconnecting", 15))
        time.sleep(max(0.0, 15 - 12))
        self.proxy.down = False
        self.assertTrue(wait_for(lambda: self.page.connects > connects and self.page.state == "ok", 30))
        self.assertGreaterEqual(self.page.resumes, 1, "away past 10 s: back by the signed resume token")
        self.assertIn("said while Alex was away", self.page.messages.values())
        self.assertTrue(wait_for(lambda: self.state() == "ok", 15))
        self.assertEqual(self.party.pending_requests(), [], "no new approval")
        self.assertTrue(self.present())

    def test_a_new_address_mid_session_is_followed(self):
        old = self.proxy
        old.close()  # this way is gone for good (a new public address, a tunnel that timed out)
        self.ways.append(self.make_proxy())  # the ladder finds another
        port = self.link.port
        self.party.chat(self.sam, "after the move")
        self.assertTrue(wait_for(lambda: "after the move" in self.page.messages.values(), 30))
        self.assertEqual(self.link.port, port, "the window's own address did not change")
        self.assertEqual(self.link.way, ("127.0.0.1", self.ways[-1].port))

    def test_a_network_change_on_this_pc_reconnects_at_once(self):
        connects = self.page.connects
        started = time.monotonic()
        self.link.network_changed()
        self.assertTrue(wait_for(lambda: self.page.connects > connects, 8))
        self.assertLess(time.monotonic() - started, 8, "not after the 10 s a silent host takes")

    def test_a_member_banned_while_away_cannot_come_back(self):
        self.proxy.down = True
        self.assertTrue(wait_for(lambda: self.state() == "reconnecting", 15))
        self.party.ban(self.party.host_id, self.member)
        self.proxy.down = False
        self.assertTrue(wait_for(lambda: self.page.ended == "gone", 30))
        self.assertFalse(self.present())


class GraceTests(Base):
    grace = 20.0

    def test_away_longer_than_the_grace_is_out_cleanly_and_rejoining_works(self):
        self.proxy.down = True
        self.assertTrue(wait_for(lambda: not self.present(), self.grace + 15), "out once the grace ran out")
        self.proxy.down = False
        self.assertTrue(wait_for(lambda: self.page.ended == "gone", 45), "the page knows its place has ended")
        # Rejoin: ask again as from the control panel. Someone who was let in before comes straight in.
        nonce = self.party.challenge()
        ticket = self.party.ask("Alex", "", "203.0.113.5", self.identity.card("Alex"), nonce,
                                self.identity.sign(ask_message(self.party.fingerprint, nonce, "Alex")).hex())
        answer = self.party.answer(ticket)
        self.assertEqual(answer["state"], "allowed")
        self.assertIsNotNone(self.party.redeem(answer["token"]))
        self.assertEqual(self.party.pending_requests(), [])

    def test_the_link_gives_up_once_the_grace_is_over(self):
        link = PartyLink(lambda force: (_ for _ in ()).throw(OSError("nothing answers")), name="Nowhere", grace=1.5)
        self.addCleanup(link.close)
        started = time.monotonic()
        with self.assertRaises(LinkLost):
            link.connect()
        self.assertEqual(link.state, "lost")
        self.assertLess(time.monotonic() - started, 6)


class ControllerLinkTests(unittest.TestCase):
    """The app's side: a joined window's link, the ladder it finds the host again by, and resuming through the app."""

    def setUp(self):
        from privatepartynet.app import Controller
        from test_app import FakeGrabber, free_port

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.host = Controller(data_folder=folder.name + "/host", discovery_port=0, grabber=FakeGrabber())
        self.guest = Controller(data_folder=folder.name + "/guest", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.host.shutdown)
        self.addCleanup(self.guest.shutdown)
        self.assertTrue(self.host.start("Host", free_port())["ok"])
        self.party = self.host._party
        self.pins = [self.party.fingerprint, self.party.spki]

    def get(self, link, path):
        conn = connect._connect(link.address, link.port, self.pins, 15.0)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def entry(self, way, token="", key=""):
        port = self.party.port
        invite = {"name": "Host", "candidates": [["lan", "127.0.0.1", port]], "fingerprint": self.party.fingerprint}
        data = {"url": f"https://127.0.0.1:{port}/join?token=x", "name": "Host", "key": key,
                "link": {"way": way, "invite": invite, "resume_token": token, "key": ""}}
        entry = self.guest._open_link(data, self.pins)
        self.addCleanup(entry["link"].close)
        return entry

    def test_a_servers_room_keeps_its_address_on_this_pc_from_one_start_to_the_next(self):
        # A room keeps its settings on this PC per origin (voice devices, volumes, muted channels, Go Live), and the game
        # network reaches the host through the same address: a new address each start lost all of that.
        way = ("127.0.0.1", self.party.port)
        first = self.entry(way, key="joined-heroes")
        base = first["link"].base
        first["link"].close()
        self.assertEqual(self.entry(way, key="joined-heroes")["link"].base, base)
        self.assertNotEqual(self.entry(way, key="joined-other")["link"].base, base, "each server has its own")

    def test_joining_again_while_the_room_is_open_keeps_its_address(self):
        opened = []
        self.guest._shell = type("Shell", (), {"stop": lambda self: None})()  # the one window, as far as _join needs
        self.guest._shell_room = lambda key, name, url, origin, **options: opened.append(origin)
        port = self.party.port
        data = {"url": f"https://127.0.0.1:{port}/join?token=x", "name": "Host", "key": "joined-heroes",
                "pins": tuple(self.pins), "link": {"way": ("127.0.0.1", port), "invite": {
                    "name": "Host", "candidates": [["lan", "127.0.0.1", port]], "fingerprint": self.party.fingerprint}}}
        self.guest._join(dict(data))
        self.guest._join(dict(data))  # Try again, or a rejoin after the grace
        self.assertEqual(len(opened), 2)
        self.assertEqual(opened[0], opened[1])
        self.assertEqual(self.get(self.guest._links["joined-heroes"]["link"], "/static/rtc.js")[0], 200)

    def test_a_joined_window_loads_the_party_through_its_own_address_on_this_pc(self):
        entry = self.entry(("127.0.0.1", self.party.port))
        link = entry["link"]
        self.assertTrue(link.address.startswith("127.84."), "its own loopback address, so no two parties share cookies")
        self.assertEqual(self.get(link, "/static/rtc.js")[0], 200)
        self.assertEqual(self.guest._path_name({"way": ("192.168.0.12", 8765)}), "direct-lan")
        self.assertEqual(self.guest._path_name({"way": ("127.0.0.1", 9), "tunnel": type("T", (), {"kind": "relay"})()}),
                         "relay")

    def test_a_way_that_broke_is_replaced_from_the_ladder(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        entry = self.entry(("127.0.0.1", dead_port))  # as if the address the window started on went away
        self.assertEqual(self.get(entry["link"], "/static/rtc.js")[0], 200)
        self.assertEqual(entry["way"], ("127.0.0.1", self.party.port), "found again among the invite's addresses")

    def test_the_app_resumes_a_member_the_host_wants_to_see_signed(self):
        token = self.party.join_token("Guest", user=self.guest._identity.user_id, caps=["link"])
        resume = self.party._join_resume[token]
        member = self.party.member_for(self.party.redeem(token))
        self.party._members[member]["suspended"] = True  # away past 10 s
        entry = self.entry(("127.0.0.1", self.party.port), token=resume)
        self.guest._name = "Guest"
        self.assertEqual(entry["resume"](), {"ok": True, "message": "Reconnected."})
        self.assertFalse(self.party.suspended(member))
        self.party.ban(self.party.host_id, member)
        result = entry["resume"]()
        self.assertEqual((result["ok"], result["gone"]), (False, True))


if __name__ == "__main__":
    unittest.main()
