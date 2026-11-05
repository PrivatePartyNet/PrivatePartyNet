"""The other end hanging up mid-request (a party window restarting its frame feed, a member dropping out) closes that
connection quietly: the party keeps running, the control panel shows nothing, and the log has an INFO line saying which
connection it was. A real fault is logged with its traceback and shown in plain words, never as the exception itself.
This is what showed "ConnectionAbortedError: [WinError 10053] ..." in 3.1.0's control panel."""

import http.client
import logging
import socket
import ssl
import struct
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet.cert import ensure_certificate
from privatepartynet.feed import FeedServer
from privatepartynet.hangups import describe, is_hangup
from privatepartynet.server import Party


class Grabber:
    """Frames without end, each big enough that a hung-up reader leaves the server writing into a closed socket."""

    def frames(self, fps, max_height, pointer=True):
        while True:
            yield b"\xff\xd8" + bytes(256 * 1024) + b"\xff\xd9"
            time.sleep(0.005)

    def monitors(self):
        return [{"index": 0, "width": 1920, "height": 1080}]

    def for_monitor(self, monitor):
        return self


def drop(sock):
    """Hang up hard, the way a closed window's connection is reset: no goodbye, a TCP reset."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def wait_for(condition, seconds=5.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class ClassifyTests(unittest.TestCase):
    def test_hangups_are_told_apart_from_faults(self):
        for exc in (ConnectionAbortedError(10053, "aborted"), ConnectionResetError(), BrokenPipeError(),
                    ssl.SSLEOFError(), OSError(10058, "shut down", None, 10058)):
            self.assertTrue(is_hangup(exc), exc)
        for exc in (ValueError("x"), OSError(10048, "in use", None, 10048), FileNotFoundError()):
            self.assertFalse(is_hangup(exc), exc)
        self.assertEqual(describe(OSError(10053, "aborted", None, 10053)), "WinError 10053")


class PartyHangupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.party = Party("000000", 0, certfile, keyfile, party_name="Hangups", host_name="Host", grabber=Grabber(),
                           heartbeat=0.1)
        self.party.start()
        self.addCleanup(self.party.stop)
        self.logs = self.enterContext(self.assertLogs("privatepartynet.connections", level="INFO"))

    def raw(self, port=None):
        sock = socket.create_connection(("127.0.0.1", port or self.party.local_port), timeout=5)
        return sock

    def request(self, sock, path, cookie):
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\n\r\n".encode())

    def assert_quiet(self):
        self.assertTrue(wait_for(lambda: any(("closed" in line or "ended" in line) for line in self.logs.output)), self.logs.output)
        self.assertEqual(self.party.last_error, "", "nothing reaches the control panel")
        self.assertTrue(all(line.startswith("INFO:") for line in self.logs.output), self.logs.output)
        self.assertFalse(self.party.closing, "the party keeps running")
        connection = http.client.HTTPConnection("127.0.0.1", self.party.local_port, timeout=5)
        connection.request("GET", "/static/rtc.js")
        self.assertEqual(connection.getresponse().status, 200, "and keeps answering")
        connection.close()

    def test_a_window_that_drops_its_frame_feed_mid_stream(self):
        sock = self.raw()
        self.request(sock, "/frames?fps=30&height=720", f"privatepartynet_host={self.party.host_token}")
        sock.recv(65536)  # the feed has started
        drop(sock)
        self.assert_quiet()

    def test_go_live_restarting_the_feed_again_and_again_at_other_sizes(self):
        for height in (720, 480, 1080, 720, 480, 1080, 720):  # what the log of the real run showed, seven times in 30 s
            sock = self.raw()
            self.request(sock, f"/frames?fps=30&height={height}", f"privatepartynet_host={self.party.host_token}")
            sock.recv(65536)
            drop(sock)
        self.assert_quiet()

    def test_a_member_that_drops_its_event_stream(self):
        session = self.party.redeem(self.party.join_token("Alex"))
        sock = self.raw()
        self.request(sock, "/events", f"privatepartynet_member={session}")
        sock.recv(65536)
        drop(sock)
        time.sleep(0.5)  # a few heartbeats are written into the reset socket
        self.assert_quiet()

    def test_a_member_that_hangs_up_while_a_page_is_being_sent(self):
        session = self.party.redeem(self.party.join_token("Alex"))
        for _ in range(5):
            sock = self.raw()
            self.request(sock, "/static/room.js", f"privatepartynet_member={session}")
            drop(sock)  # before reading a byte of the answer
        time.sleep(0.5)
        self.assertEqual(self.party.last_error, "")
        self.assertFalse(self.party.closing)


class WinError10053Tests(unittest.TestCase):
    def test_a_member_whose_connection_windows_gave_up_on_shows_nothing_in_the_control_panel(self):
        """3.1.0 showed exactly this exception in the control panel: Windows aborts a connection to a member who has
        stopped answering (on Starlink, here), and the next send on it raises WinError 10053."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        party = Party("000000", 0, certfile, keyfile, party_name="Starlink", host_name="Host")
        party.start()
        self.addCleanup(party.stop)
        from privatepartynet import server

        def aborted(handler):
            raise ConnectionAbortedError(10053, "An established connection was aborted by the software in your host "
                                                "machine", None, 10053)

        original = server._Handler._cpu
        server._Handler._cpu = aborted
        self.addCleanup(setattr, server._Handler, "_cpu", original)
        with self.assertLogs("privatepartynet.connections", level="INFO") as logs:
            connection = http.client.HTTPConnection("127.0.0.1", party.local_port, timeout=5)
            connection.request("GET", "/cpu")
            try:
                connection.getresponse()
            except (http.client.HTTPException, OSError):
                pass
            connection.close()
            self.assertTrue(wait_for(lambda: any("10053" in line for line in logs.output)), logs.output)
        self.assertEqual(party.last_error, "")
        self.assertTrue(all(line.startswith("INFO:") for line in logs.output), logs.output)
        self.assertIn("GET /cpu", "\n".join(logs.output), "the log says which connection it was")
        self.assertFalse(party.closing)


class FaultTests(unittest.TestCase):
    def test_a_real_fault_is_logged_with_its_traceback_and_shown_in_plain_words(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        party = Party("000000", 0, certfile, keyfile, party_name="Faults", host_name="Host")
        party.start()
        self.addCleanup(party.stop)

        def broken():
            raise ValueError("something unexpected")

        from privatepartynet import server

        original = server._Handler._cpu
        server._Handler._cpu = lambda handler: broken()
        self.addCleanup(setattr, server._Handler, "_cpu", original)
        with self.assertLogs("privatepartynet.connections", level="ERROR") as logs:
            connection = http.client.HTTPConnection("127.0.0.1", party.local_port, timeout=5)
            connection.request("GET", "/cpu")
            try:
                connection.getresponse()
            except (http.client.HTTPException, OSError):
                pass
            connection.close()
            wait_for(lambda: party.last_error)
        self.assertIn("ValueError: something unexpected", "\n".join(logs.output), "the traceback is in the log")
        self.assertIn("details are in the log", party.last_error)
        self.assertNotIn("ValueError", party.last_error, "no exception in the control panel")


class FeedHangupTests(unittest.TestCase):
    def test_the_frame_feed_for_a_joined_party_window_hangs_up_quietly_too(self):
        feed = FeedServer(Grabber())
        feed.start()
        self.addCleanup(feed.stop)
        token = feed.issue()
        with self.assertLogs("privatepartynet.connections", level="INFO") as logs:
            sock = socket.create_connection(("127.0.0.1", feed.port), timeout=5)
            sock.sendall(f"GET /frames?token={token}&fps=30 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
            sock.recv(65536)
            drop(sock)
            self.assertTrue(wait_for(lambda: any("frame feed" in line for line in logs.output)))
        self.assertTrue(all(line.startswith("INFO:") for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
