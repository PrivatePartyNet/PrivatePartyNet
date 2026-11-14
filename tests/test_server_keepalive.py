"""An answered connection to the party carries the page's next request (HTTP/1.1 keep-alive), so the heartbeat's answer
(POST /pong) does not pay for a new TLS handshake each time and the ping shown is one round trip, not three."""

import http.client
import json
import socket
import threading
import time
import unittest
from unittest import mock

from privatepartynet import server as server_module
from privatepartynet.identity import Identity
from test_server import JSON_HEADERS, insecure_context, read_until
from test_server_servers import ServerTestCase


class SlowLink:
    """A TCP relay on 127.0.0.1 that holds everything it carries for `delay` seconds each way, like a far-off host."""

    def __init__(self, target, delay):
        self.target, self.delay = target, delay
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self._open = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            upstream = socket.create_connection(self.target)
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._open += [client, upstream]
            threading.Thread(target=self._carry, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._carry, args=(upstream, client), daemon=True).start()

    def _carry(self, source, target):
        pending = []  # (due, data), in order
        ready = threading.Condition()
        done = []

        def send():
            while True:
                with ready:
                    while not pending and not done:
                        ready.wait()
                    if not pending:
                        break
                    due, data = pending.pop(0)
                time.sleep(max(0.0, due - time.monotonic()))
                try:
                    target.sendall(data)
                except OSError:
                    return
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=send, daemon=True).start()
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                with ready:
                    pending.append((time.monotonic() + self.delay, data))
                    ready.notify()
        except OSError:
            pass
        with ready:
            done.append(True)
            ready.notify()

    def close(self):
        self.listener.close()
        for sock in self._open:
            try:
                sock.close()
            except OSError:
                pass


class KeepAliveTests(ServerTestCase):
    def connection(self, party, port=None):
        conn = http.client.HTTPSConnection("127.0.0.1", port or party.port, timeout=5, context=insecure_context())
        self.addCleanup(conn.close)
        return conn

    def test_an_answered_connection_carries_the_next_request(self):
        party = self.party()
        conn = self.connection(party)
        conn.request("GET", "/static/rtc.js")
        first = conn.getresponse()
        first.read()
        self.assertFalse(first.will_close)
        sock = conn.sock
        for path in ("/static/room.js", "/ask?ticket=x"):
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        self.assertIs(conn.sock, sock, "the same connection, with no new handshake")

    def test_a_refused_body_is_read_so_the_connection_goes_on(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        conn = self.connection(party)
        cookie = f"{party.cookie_name('privatepartynet_member')}={session}"
        conn.request("POST", "/name", body=json.dumps({"name": "Mallory"}), headers={"Cookie": cookie, "Content-Type": "text/plain"})
        refused = conn.getresponse()
        refused.read()
        self.assertEqual(refused.status, 403)
        sock = conn.sock
        conn.request("POST", "/name", body=json.dumps({"name": "Alexandra"}), headers={**JSON_HEADERS, "Cookie": cookie})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 202, "the next request was read from where it starts")
        self.assertIs(conn.sock, sock)
        self.assertIn("Alexandra", [m["name"] for m in party.roster()])

    def test_a_stream_says_its_connection_ends_with_it(self):
        party = self.party()
        _, session = self.member(party, "Alex")
        stream = self.stream(party, session)
        self.addCleanup(stream.close)
        self.assertEqual(stream.getheader("Connection"), "close")
        self.assertIsNone(stream.getheader("Content-Length"))

    def test_a_connection_left_idle_is_closed(self):
        with mock.patch.object(server_module, "KEEP_ALIVE_SECONDS", 0.3):
            party = self.party()
            raw = insecure_context().wrap_socket(socket.create_connection(("127.0.0.1", party.port), timeout=5),
                                                 server_hostname="127.0.0.1")
            self.addCleanup(raw.close)
            raw.sendall(b"GET /ask?ticket=x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            data = b""
            started = None
            while True:
                chunk = raw.recv(65536)
                if not chunk:
                    break
                data += chunk
                if started is None and b"\r\n\r\n" in data:
                    started = time.monotonic()
            self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:40])
            self.assertLess(time.monotonic() - started, 3, "closed after KEEP_ALIVE_SECONDS, not the minute a request has")

    def test_a_crowded_server_closes_answered_connections(self):
        with mock.patch.object(server_module, "MAX_CONNECTIONS", 4):
            party = self.party()
        idle = [socket.create_connection(("127.0.0.1", party.port), timeout=5) for _ in range(2)]
        self.addCleanup(lambda: [s.close() for s in idle])
        time.sleep(0.3)  # two of four slots are taken; with this one, three
        conn = self.connection(party)
        conn.request("GET", "/ask?ticket=x")
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.will_close, "past 3/4 of the slots, an answered connection is not kept")

    def test_the_heartbeat_over_a_far_link_takes_one_round_trip(self):
        """50 ms each way: a ping and its answer take 100 ms. On a new connection each time the answer waited for a
        TLS handshake too (two more crossings), which the ping shown used to include."""
        party = self.party(ping_seconds=3600)
        link = SlowLink(("127.0.0.1", party.port), 0.05)
        self.addCleanup(link.close)
        identity = Identity.create()
        session = party.redeem(party.join_token("Alex", user=identity.user_id, caps=["link"]))
        cookie = {"Cookie": f"{party.cookie_name('privatepartynet_member')}={session}"}
        events = self.connection(party, link.port)
        events.request("GET", "/events", headers=cookie)
        stream = events.getresponse()
        read_until(stream, "hello")
        page = self.connection(party, link.port)  # the page's other connection, warm from loading the room
        page.request("GET", "/static/rtc.js")
        page.getresponse().read()
        shown = []
        for _ in range(3):
            party.pulse()
            ping = read_until(stream, "ping")
            page.request("POST", "/pong", body=json.dumps({"id": ping["id"]}), headers={**JSON_HEADERS, **cookie})
            response = page.getresponse()
            self.assertEqual(response.status, 200)
            shown.append(json.loads(response.read())["rtt"])
        self.assertEqual(link.connections, 2, "the answers all went on the warm connection")
        self.assertGreaterEqual(min(shown), 85)
        self.assertLess(max(shown), 170, f"the ping shown, ms: {shown}")


if __name__ == "__main__":
    unittest.main()
