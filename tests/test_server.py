import http.client

import json

import ssl

import tempfile

import time

import unittest



from privatepartynet.cert import ensure_certificate

from privatepartynet.server import MAX_MEMBERS, Party



PASSCODE = "428617"

FORM = {"Content-Type": "application/x-www-form-urlencoded"}

JSON_HEADERS = {"Content-Type": "application/json"}





def insecure_context():

    """The tests trust their own throwaway certificate, so skip verification."""

    context = ssl.create_default_context()

    context.check_hostname = False

    context.verify_mode = ssl.CERT_NONE

    return context





def next_event(response):

    """Return the next decoded data: event from an event stream, skipping keep-alive comments."""

    while True:

        line = response.readline()

        if not line:

            raise AssertionError("event stream closed")

        if line.startswith(b"data: "):

            return json.loads(line[len(b"data: "):])





def read_until(response, kind, test=lambda event: True, seconds=15.0):

    """Read events until one of this kind (and passing test) arrives. The party's heartbeat keeps the socket busy, so
    the socket timeout alone would never end the wait; `seconds` does."""

    ends = time.monotonic() + seconds

    while True:

        event = next_event(response)

        if event.get("type") == kind and test(event):

            return event

        if time.monotonic() > ends:

            raise AssertionError(f"no {kind} event like that within {seconds:.0f} seconds")





def online_names(roster):

    return sorted(m["name"] for m in roster["members"] if m["online"])





class PartyTests(unittest.TestCase):

    def setUp(self):

        folder = tempfile.TemporaryDirectory()

        self.addCleanup(folder.cleanup)

        certfile, keyfile = ensure_certificate(folder.name, ["localhost", "127.0.0.1"])

        self.party = Party(PASSCODE, 0, certfile, keyfile, party_name="Test party", host_name="Host",

                           failed_login_delay=0, heartbeat=0.2)

        self.party.start()

        self.addCleanup(self.party.stop)



    def open(self, method, path, body=None, headers=None):

        """Send one request on its own connection. Returns (connection, response) so streams can be closed."""

        conn = http.client.HTTPSConnection("127.0.0.1", self.party.port, timeout=5, context=insecure_context())

        self.addCleanup(conn.close)

        conn.request(method, path, body=body, headers=headers or {})

        return conn, conn.getresponse()



    def join(self, name):
        conn, response = self.open("GET", f"/join?token={self.party.join_token(name)}")
        if response.status != 303:
            return None
        cookie = response.getheader("Set-Cookie")
        self.assertIn("Secure", cookie)
        response.read()
        conn.close()
        return cookie.split(";", 1)[0]
    def host_cookie(self):

        conn, response = self.open("GET", "/host")

        self.assertEqual(response.status, 200)

        cookie = response.getheader("Set-Cookie").split(";", 1)[0]

        response.read()

        conn.close()

        return cookie



    def stream(self, cookie):

        viewer = cookie.startswith("privatepartynet_viewer_")  # a pop-out page says so on its event stream
        _, response = self.open("GET", "/events?as=viewer" if viewer else "/events", headers={"Cookie": cookie})

        self.assertEqual(response.status, 200)

        # The response owns the socket. A caller that drops it would close the page, and the party would mark the
        # member offline and end their shares partway through the test, so the stream stays open until the test ends.
        self.addCleanup(response.close)

        return response



    def post(self, path, body, cookie):

        marker = {"X-PrivatePartyNet-View": "1"} if cookie.startswith("privatepartynet_viewer_") else {}

        _, response = self.open("POST", path, body=json.dumps(body), headers={**JSON_HEADERS, **marker, "Cookie": cookie})

        status = response.status

        response.read()

        return status



    def test_signed_out_root_only_explains_how_to_join(self):
        _, response = self.open("GET", "/")
        self.assertEqual(response.status, 200)
        body = response.read().decode()
        self.assertIn("Open this party from the PrivatePartyNet app", body)
        self.assertNotIn('name="passcode"', body)
    def test_a_join_link_works_once_and_never_in_a_browser_form(self):
        _, response = self.open("GET", "/join?token=not-a-real-token")
        self.assertEqual(response.status, 403)
        response.read()
        token = self.party.join_token("Eve")
        _, response = self.open("GET", f"/join?token={token}")
        self.assertEqual(response.status, 303)
        response.read()
        _, response = self.open("GET", f"/join?token={token}")
        self.assertEqual(response.status, 403)
        response.read()
    def test_events_need_a_session(self):

        _, response = self.open("GET", "/events")

        self.assertEqual(response.status, 401)

        response.read()



    def test_host_page_opens_from_the_host_pc_only_and_sets_the_host_cookie(self):

        cookie = self.host_cookie()

        self.assertTrue(cookie.startswith("privatepartynet_host_"))



    def test_hello_tells_each_member_who_they_are(self):

        host = self.stream(self.host_cookie())

        hello = read_until(host, "hello")

        self.assertEqual(hello["you"]["role"], "host")

        self.assertEqual(hello["party"], "Test party")

        self.assertEqual(hello["you"]["name"], "Host")



        alex = self.stream(self.join("Alex"))

        hello = read_until(alex, "hello")

        self.assertEqual(hello["you"]["role"], "member")

        self.assertEqual(hello["you"]["name"], "Alex")



    def test_roster_shows_everyone_online(self):

        host = self.stream(self.host_cookie())

        read_until(host, "hello")

        alex = self.stream(self.join("Alex"))

        read_until(alex, "hello")

        roster = read_until(host, "roster", lambda e: len(online_names(e)) == 2)

        self.assertEqual(online_names(roster), ["Alex", "Host"])

        self.assertEqual(roster["host"], self.party.host_id)



    def test_a_chat_between_two_people_is_shown_to_someone_who_joins_later(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        alex = self.stream(alex_cookie)
        read_until(alex, "hello")
        self.assertEqual(self.post("/chat", {"text": "just us"}, alex_cookie), 202)
        self.assertEqual(read_until(host, "chat")["text"], "just us")
        self.assertEqual(read_until(alex, "chat")["name"], "Alex")

        late = self.stream(self.join("Sam"))
        read_until(late, "hello")
        self.assertEqual([m["text"] for m in read_until(late, "history")["messages"]], ["just us"])

    def test_a_party_message_with_three_people_is_shown_to_someone_who_joins_later(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        alex = self.stream(alex_cookie)
        read_until(alex, "hello")
        sam = self.stream(self.join("Sam"))  # kept, so the connection stays open
        read_until(sam, "hello")
        self.assertEqual(self.post("/chat", {"text": "hello party"}, alex_cookie), 202)
        self.assertEqual(read_until(host, "chat")["text"], "hello party")

        late = self.stream(self.join("Dana"))
        read_until(late, "hello")
        self.assertEqual([m["text"] for m in read_until(late, "history")["messages"]], ["hello party"])

    def test_chat_refuses_empty_text(self):

        cookie = self.join("Alex")

        self.assertEqual(self.post("/chat", {"text": "   "}, cookie), 400)



    def test_going_live_shows_in_everyones_roster(self):

        host = self.stream(self.host_cookie())

        read_until(host, "hello")

        alex_cookie = self.join("Alex")

        self.stream(alex_cookie)

        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True, "label": "Screen 1", "width": 800, "height": 450}, alex_cookie), 202)

        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and m["sharing"] for m in e["members"]))

        self.assertTrue(any(m["name"] == "Alex" and m["sharing"] for m in roster["members"]))

    def test_one_person_can_share_several_screens_and_stop_them_one_by_one(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        self.stream(alex_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True, "label": "Screen 1", "width": 800, "height": 450}, alex_cookie), 202)
        self.assertEqual(self.post("/share", {"slot": 2, "sharing": True, "label": "Screen 3", "width": 1920, "height": 1080}, alex_cookie), 202)
        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and len(m["shares"]) == 2 for m in e["members"]))
        alex = next(m for m in roster["members"] if m["name"] == "Alex")
        self.assertEqual(alex["shares"], [
            {"slot": 0, "label": "Screen 1", "width": 800, "height": 450},
            {"slot": 2, "label": "Screen 3", "width": 1920, "height": 1080},
        ])
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": False}, alex_cookie), 202)
        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and len(m["shares"]) == 1 for m in e["members"]))
        alex = next(m for m in roster["members"] if m["name"] == "Alex")
        self.assertEqual([s["slot"] for s in alex["shares"]], [2])
        self.assertTrue(alex["sharing"])

    def test_a_share_needs_a_real_slot(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        self.stream(alex_cookie)
        for bad in (4, -1, "0", True):
            self.assertEqual(self.post("/share", {"slot": bad, "sharing": True}, alex_cookie), 400, bad)

    def test_a_label_is_printable_and_short(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        self.stream(alex_cookie)
        self.assertEqual(self.post("/share", {"slot": 1, "sharing": True, "label": "x" * 200 + ""}, alex_cookie), 202)
        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and m["shares"] for m in e["members"]))
        label = next(m for m in roster["members"] if m["name"] == "Alex")["shares"][0]["label"]
        self.assertEqual(label, "x" * 40)



    def test_signals_go_to_their_target_only(self):

        host = self.stream(self.host_cookie())

        host_id = read_until(host, "hello")["you"]["id"]

        alex_cookie = self.join("Alex")

        alex = self.stream(alex_cookie)

        alex_id = read_until(alex, "hello")["you"]["id"]

        sam_cookie = self.join("Sam")

        sam = self.stream(sam_cookie)

        read_until(sam, "hello")



        self.assertEqual(self.post("/signal", {"to": alex_id, "kind": "offer", "sdp": "v=0 offer"}, sam_cookie), 202)

        signal = read_until(alex, "signal")

        self.assertEqual((signal["kind"], signal["sdp"], signal["from"]), ("offer", "v=0 offer", signal["from"]))

        self.assertNotEqual(signal["from"], alex_id)

        self.assertNotEqual(host_id, alex_id)



    def test_signals_are_checked_and_need_an_online_target(self):

        alex_cookie = self.join("Alex")

        self.stream(alex_cookie)

        self.assertEqual(self.post("/signal", {"to": "nobody", "kind": "offer", "sdp": "x"}, alex_cookie), 409)

        self.assertEqual(self.post("/signal", {"to": "x", "kind": "bogus"}, alex_cookie), 400)

        self.assertEqual(self.post("/signal", {"to": "x", "kind": "offer"}, alex_cookie), 400)



    def test_receive_choices_are_relayed_and_bad_ones_refused(self):

        host = self.stream(self.host_cookie())

        host_id = read_until(host, "hello")["you"]["id"]

        alex_cookie = self.join("Alex")

        read_until(self.stream(alex_cookie), "hello")



        self.assertEqual(self.post("/signal", {"to": host_id, "slot": 0, "kind": "quality", "mode": "percent", "percent": 50}, alex_cookie), 202)

        signal = read_until(host, "signal")

        self.assertEqual((signal["kind"], signal["percent"]), ("quality", 50))



        self.assertEqual(self.post("/signal", {"to": host_id, "slot": 0, "kind": "quality", "mode": "fit", "width": 800, "height": 450}, alex_cookie), 202)

        self.assertEqual(read_until(host, "signal")["width"], 800)



        refused = [

            {"to": host_id, "slot": 0, "kind": "quality", "mode": "percent", "percent": 33},

            {"to": host_id, "slot": 0, "kind": "quality", "mode": "fit", "width": "800", "height": 450},

            {"to": host_id, "slot": 0, "kind": "quality", "mode": "fit", "width": 99999, "height": 450},

            {"to": host_id, "slot": 0, "kind": "quality", "mode": "fit", "width": True, "height": 450},

            {"to": host_id, "slot": 0, "kind": "quality", "mode": "fit", "width": 10, "height": 450},

        ]

        for bad in refused:

            self.assertEqual(self.post("/signal", bad, alex_cookie), 400, bad)



    def test_the_host_can_remove_a_member_and_nobody_else_can(self):

        host_cookie = self.host_cookie()

        read_until(self.stream(host_cookie), "hello")

        alex_cookie = self.join("Alex")

        alex = self.stream(alex_cookie)

        alex_id = read_until(alex, "hello")["you"]["id"]

        sam_cookie = self.join("Sam")

        sam_id = read_until(self.stream(sam_cookie), "hello")["you"]["id"]



        self.assertEqual(self.post("/kick", {"id": sam_id}, alex_cookie), 403)

        self.assertEqual(self.post("/kick", {"id": alex_id}, host_cookie), 202)

        self.assertEqual(read_until(alex, "kicked")["type"], "kicked")

        self.assertEqual(self.post("/chat", {"text": "still here?"}, alex_cookie), 400)



    def test_members_can_leave_and_their_session_ends(self):

        cookie = self.join("Alex")

        self.stream(cookie)

        self.assertEqual(self.post("/leave", {}, cookie), 202)

        _, response = self.open("GET", "/events", headers={"Cookie": cookie})

        self.assertEqual(response.status, 401)

        response.read()



    def test_the_host_cannot_leave_their_own_party(self):

        cookie = self.host_cookie()

        self.assertEqual(self.post("/leave", {}, cookie), 403)



    def test_the_party_holds_sixteen_people_including_the_host(self):

        for index in range(MAX_MEMBERS - 1):

            self.assertIsNotNone(self.join(f"Guest {index}"))

        self.assertIsNone(self.join("One too many"))



    def test_ending_the_party_tells_everyone_and_ends_their_streams(self):

        host = self.stream(self.host_cookie())

        read_until(host, "hello")

        alex = self.stream(self.join("Alex"))

        read_until(alex, "hello")

        self.party.stop()

        self.assertEqual(read_until(alex, "stop")["type"], "stop")



    def test_names_are_cleaned_and_rename_reaches_the_roster(self):

        cookie = self.join("  Alex\x00  ")

        self.assertEqual(self.post("/name", {"name": "Alexandra"}, cookie), 202)

        self.assertEqual([m["name"] for m in self.party.roster() if m["name"] != "Host"], ["Alexandra"])





    def test_a_post_from_another_page_is_refused(self):
        """Rooms run in frames of the app's one window, beside other servers' rooms, and an HTTPS party's cookie is
        SameSite=None so it reaches its frame. So a POST that a page from another address sends is refused."""
        cookie = self.join("Alex")
        own = f"https://127.0.0.1:{self.party.port}"
        for origin in ("https://192.168.1.50:8765", "http://127.0.0.1:9999", "null", f"http://127.0.0.1:{self.party.port}"):
            _, response = self.open("POST", "/name", body=json.dumps({"name": "Mallory"}),
                                    headers={**JSON_HEADERS, "Cookie": cookie, "Origin": origin})
            with self.subTest(origin=origin):
                self.assertEqual(response.status, 403)
                response.read()
        self.assertNotIn("Mallory", [m["name"] for m in self.party.roster()])
        _, response = self.open("POST", "/name", body=json.dumps({"name": "Alexandra"}),
                                headers={**JSON_HEADERS, "Cookie": cookie, "Origin": own})
        self.assertEqual(response.status, 202, "the party's own page is let through")

    def test_a_post_not_declared_as_json_is_refused(self):
        """A plain form or text POST needs no permission from another page; the party's own pages always declare JSON."""
        cookie = self.join("Alex")
        for declared in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None):
            headers = {"Cookie": cookie}
            if declared:
                headers["Content-Type"] = declared
            _, response = self.open("POST", "/name", body=json.dumps({"name": "Mallory"}), headers=headers)
            with self.subTest(declared=declared):
                self.assertEqual(response.status, 403)
                response.read()
        self.assertNotIn("Mallory", [m["name"] for m in self.party.roster()])

    def test_an_https_cookie_reaches_a_frame_and_the_loopback_one_stays_strict(self):
        """The app's window is a page on 127.0.0.1; an HTTPS party's room is a frame in it, another site, so its cookie
        is SameSite=None (and Secure). The host's own room is plain HTTP on 127.0.0.1, the same site: Strict stays."""
        import http.client as hc

        _, response = self.open("GET", f"/join?token={self.party.join_token('Alex')}")
        joined = response.getheader("Set-Cookie")
        self.assertIn("Secure", joined)
        self.assertIn("SameSite=None", joined)
        self.assertIn("HttpOnly", joined)
        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/host")
        host = conn.getresponse()
        host.read()
        self.assertIn("SameSite=Strict", host.getheader("Set-Cookie"))

    def test_the_host_page_loads_over_plain_loopback_http_with_a_host_cookie(self):

        import http.client as hc

        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)

        conn.request("GET", "/host")

        response = conn.getresponse()

        body = response.read().decode()

        self.assertEqual(response.status, 200)

        self.assertIn('data-capture="frames"', body)

        cookie = response.getheader("Set-Cookie")

        self.assertTrue(cookie.startswith("privatepartynet_host_"))

        self.assertNotIn("Secure", cookie)

        conn.close()



    def test_the_host_frame_feed_streams_length_prefixed_frames(self):

        import http.client as hc

        import struct

        from privatepartynet.frames import ScreenGrabber

        self.party.grabber = type("G", (), {"frames": staticmethod(lambda fps, h, pointer=True: iter([b"frame-one", b"frame-two"]))})()
        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)

        conn.request("GET", "/host")

        response = conn.getresponse()

        cookie = response.getheader("Set-Cookie").split(";", 1)[0]

        response.read()

        conn.close()

        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)

        conn.request("GET", "/frames?fps=30&height=720", headers={"Cookie": cookie})

        response = conn.getresponse()

        self.assertEqual(response.status, 200)

        data = response.read()

        conn.close()

        frames = []

        while data:

            (size,) = struct.unpack(">I", data[:4])

            frames.append(data[4:4 + size])

            data = data[4 + size:]

        self.assertEqual(frames, [b"frame-one", b"frame-two"])


    def test_members_cannot_use_the_frame_feed(self):

        import http.client as hc

        self.party.grabber = type("G", (), {"frames": staticmethod(lambda fps, h, pointer=True: iter([b"frame-one", b"frame-two"]))})()
        cookie = self.join("Alex")

        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)

        conn.request("GET", "/frames", headers={"Cookie": cookie})

        response = conn.getresponse()

        response.read()

        self.assertEqual(response.status, 403)

        conn.close()



    def test_the_theme_reaches_every_page(self):

        self.party.theme = "dark"

        _, response = self.open("GET", "/")

        self.assertIn('data-theme="dark"', response.read().decode())





    def test_a_member_pops_a_live_screen_out_as_a_viewer_that_cannot_share(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        sam_cookie = self.join("Sam")
        self.stream(alex_cookie)
        self.stream(sam_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True, "label": "Screen 1", "width": 800, "height": 450}, alex_cookie), 202)
        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and m["sharing"] for m in e["members"]))
        alex_id = next(m["id"] for m in roster["members"] if m["name"] == "Alex")

        _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 0}),
                                headers={**JSON_HEADERS, "Cookie": sam_cookie})
        self.assertEqual(response.status, 200)
        path = json.loads(response.read())["path"]
        self.assertTrue(path.startswith("/join?token="))
        self.assertIn(f"&view={alex_id}:0", path)

        _, response = self.open("GET", path)
        self.assertEqual(response.status, 303)
        viewer_cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertTrue(viewer_cookie.startswith("privatepartynet_viewer_"), "a pop-out window gets a cookie of its own")
        response.read()
        self.stream(viewer_cookie)
        viewer_roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex window" for m in e["members"]))
        viewer = next(m for m in viewer_roster["members"] if m["name"] == "Alex window")
        self.assertEqual(viewer["role"], "viewer")
        self.assertEqual(self.post("/share", {"slot": 1, "sharing": True, "label": "Screen 2"}, viewer_cookie), 400)

        _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 3}),
                                headers={**JSON_HEADERS, "Cookie": sam_cookie})
        self.assertEqual(response.status, 400, "a screen that is not live cannot be popped out")
        response.read()
        _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 0}),
                                headers={**JSON_HEADERS, "X-PrivatePartyNet-View": "1", "Cookie": viewer_cookie})
        self.assertEqual(response.status, 400, "a viewer cannot open another viewer")
        response.read()

    def test_a_pop_out_window_does_not_take_over_the_window_that_opened_it(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        sam_cookie = self.join("Sam")
        self.stream(alex_cookie)
        self.stream(sam_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True, "label": "Screen 1"}, alex_cookie), 202)
        roster = read_until(host, "roster", lambda e: any(m["name"] == "Alex" and m["sharing"] for m in e["members"]))
        alex_id = next(m["id"] for m in roster["members"] if m["name"] == "Alex")
        sam_id = next(m["id"] for m in roster["members"] if m["name"] == "Sam")

        def pop_out():
            _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 0}),
                                    headers={**JSON_HEADERS, "Cookie": sam_cookie})
            self.assertEqual(response.status, 200, "Sam's own window can open a pop-out")
            path = json.loads(response.read())["path"]
            _, joined = self.open("GET", path)
            self.assertEqual(joined.status, 303)
            joined.read()
            return joined.getheader("Set-Cookie").split(";", 1)[0]

        # Both windows share one cookie jar, so the jar now holds Sam's cookie and the pop-out's.
        viewer_cookie = pop_out()
        both = f"{sam_cookie}; {viewer_cookie}"
        self.stream(both)  # Sam's own page still reads Sam's events
        self.assertEqual(self.post("/share", {"slot": 1, "sharing": True, "label": "Screen 2"}, both), 202,
                         "Sam can still share after the pop-out joined")
        _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 0}),
                                headers={**JSON_HEADERS, "Cookie": both})
        self.assertEqual(response.status, 200, "and open a second pop-out")
        response.read()
        _, response = self.open("GET", "/", headers={"Cookie": viewer_cookie})
        self.assertIn(b"PrivatePartyNet party", response.read(), "a pop-out can load the page on its cookie alone")
        self.assertEqual(sam_id, self.party.member_for(sam_cookie.split("=", 1)[1]))

    def test_the_host_window_keeps_working_after_its_own_pop_out_joined(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        host_cookie = self.host_cookie()
        alex_cookie = self.join("Alex")
        self.stream(alex_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True, "label": "Screen 1"}, alex_cookie), 202)
        alex_id = self.party.member_for(alex_cookie.split("=", 1)[1])

        def pop_out(cookie):
            _, response = self.open("POST", "/popout", body=json.dumps({"target": alex_id, "slot": 0}),
                                    headers={**JSON_HEADERS, "Cookie": cookie})
            status, body = response.status, response.read()
            return status, body

        status, body = pop_out(host_cookie)
        self.assertEqual(status, 200, body)
        path = json.loads(body)["path"]
        _, joined = self.open("GET", path)
        joined.read()
        viewer_cookie = joined.getheader("Set-Cookie").split(";", 1)[0]
        jar = f"{host_cookie}; {viewer_cookie}"
        status, body = pop_out(jar)
        self.assertEqual(status, 200, body)

    def test_a_refused_pop_out_says_why(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        sam_cookie = self.join("Sam")
        self.stream(alex_cookie)
        self.stream(sam_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True}, alex_cookie), 202)
        alex_id = self.party.member_for(alex_cookie.split("=", 1)[1])

        def ask(target, slot, cookie=sam_cookie):
            _, response = self.open("POST", "/popout", body=json.dumps({"target": target, "slot": slot}),
                                    headers={**JSON_HEADERS, "Cookie": cookie})
            return response.status, response.read().decode()

        status, reason = ask(alex_id, 2)
        self.assertEqual(status, 400)
        self.assertIn("no longer being shared", reason)
        status, reason = ask("nobody", 0)
        self.assertIn("left the party", reason)
        status, reason = ask(alex_id, 0, cookie=self.party.cookie_name("privatepartynet_member") + "=forged")
        self.assertIn("not signed in", reason)

    def test_a_pop_out_window_closes_when_the_member_who_opened_it_leaves(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        sam_cookie = self.join("Sam")
        self.stream(alex_cookie)
        sam_stream = self.stream(sam_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True}, alex_cookie), 202)
        alex_id = self.party.member_for(alex_cookie.split("=", 1)[1])
        sam_id = self.party.member_for(sam_cookie.split("=", 1)[1])
        path = self.party.viewer_link(sam_id, alex_id, 0)
        viewer_id = self.party.member_for(self.party.redeem(path.split("token=", 1)[1].split("&", 1)[0]))
        self.assertIn(viewer_id, [m["id"] for m in self.party.roster()])
        self.assertEqual(self.post("/leave", {}, sam_cookie), 202)
        self.assertNotIn(viewer_id, [m["id"] for m in self.party.roster()])
        self.assertNotIn(sam_id, [m["id"] for m in self.party.roster()])

    def test_a_viewer_window_closing_removes_it_from_the_roster(self):
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        alex_cookie = self.join("Alex")
        self.stream(alex_cookie)
        self.assertEqual(self.post("/share", {"slot": 0, "sharing": True}, alex_cookie), 202)
        alex_id = self.party.member_for(alex_cookie.split("=", 1)[1])
        host_id = self.party.member_for(self.host_cookie().split("=", 1)[1])
        path = self.party.viewer_link(host_id, alex_id, 0)
        self.assertIsNotNone(path)
        session = self.party.redeem(path.split("token=", 1)[1].split("&", 1)[0])
        viewer_id = self.party.member_for(session)
        listener = self.party.attach(viewer_id)
        self.assertIsNotNone(listener)
        self.party.detach(viewer_id, listener)
        self.assertNotIn(viewer_id, [m["id"] for m in self.party.roster()])

    def test_the_cpu_reading_answers_on_this_pc(self):
        _, response = self.open("GET", "/cpu")
        self.assertEqual(response.status, 200)
        value = json.loads(response.read())["app"]
        self.assertIsInstance(value, float)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_every_stylesheet_a_page_links_is_served(self):
        import re
        import http.client as hc
        page = self.open("GET", "/")[1].read().decode()
        links = re.findall(r'href="/static/([^"]+)"', page)
        self.assertIn("theme.css", links)
        conn = hc.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)
        for name in links:
            conn.request("GET", f"/static/{name}")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200, name)
            self.assertIn("text/css", response.getheader("Content-Type"), name)
        conn.close()


if __name__ == "__main__":

    unittest.main()

