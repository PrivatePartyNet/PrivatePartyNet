"""This PC's own screen feed, for a party window opened by this app.

A join window loads the party from the host's PC. Its screen picker is not available inside this app's window, so
the party page reads frames from this feed instead. The feed listens on the loopback address only, answers only
with the join token it was given, and lets the party page read it across origins. Chromium's Private Network
Access rules need that explicit answer, so the preflight answers with the private-network header too.
"""

import hmac
import json
import secrets
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from privatepartynet.cpu import sample_detail as cpu_detail
from privatepartynet.frames import clamp_fps, clamp_height, list_monitors
from privatepartynet.hangups import HangupsLogged, describe, is_hangup

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Private-Network": "true",
}


class FeedServer:
    def __init__(self, grabber):
        self.grabber = grabber
        self.token = None
        self._httpd = _FeedServer(("127.0.0.1", 0), _FeedHandler)
        self._httpd.feed = self
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="privatepartynet-feed", daemon=True)

    @property
    def port(self):
        return self._httpd.server_address[1]

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def issue(self):
        """A new token for a new join. The previous token stops working."""
        self.token = secrets.token_urlsafe(24)
        return self.token

    def allows(self, token):
        return bool(self.token) and token is not None and hmac.compare_digest(token, self.token)

    def monitors(self):
        return self.grabber.monitors() if self.grabber is not None and hasattr(self.grabber, "monitors") else list_monitors()


class _FeedServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if is_hangup(exc):
            _FeedHandler.hangup_log.info("The frame feed's connection closed early (%s)", describe(exc))
        else:
            _FeedHandler.hangup_log.error("The frame feed could not answer a request", exc_info=True)


class _FeedHandler(HangupsLogged, BaseHTTPRequestHandler):
    server_version = "PartyFeed"
    hangup_what = "frame feed"

    def log_message(self, format, *args):
        pass

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        for name, value in CORS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        feed = self.server.feed
        if not feed.allows(query.get("token", [None])[0]):
            self._send(403, b"This feed only answers the party window it was opened for.")
            return
        if url.path == "/monitors":
            self._send(200, json.dumps(feed.monitors()).encode("utf-8"), "application/json; charset=utf-8")
            return
        if url.path == "/cpu":
            self._send(200, json.dumps(cpu_detail()).encode("utf-8"), "application/json; charset=utf-8")
            return
        if url.path != "/frames":
            self._send(404, b"Not found.")
            return
        source = feed.grabber
        monitor_raw = query.get("monitor", [None])[0]
        if monitor_raw is not None:
            try:
                monitor = int(monitor_raw)
            except ValueError:
                self._send(400, b"No such screen.")
                return
            if monitor not in {m["index"] for m in feed.monitors()}:
                self._send(400, b"No such screen.")
                return
            source = feed.grabber.for_monitor(monitor)
        fps = clamp_fps(query.get("fps", [None])[0])
        height = clamp_height(query.get("height", [None])[0])
        self.send_response(200)
        for name, value in CORS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        frames = source.frames(fps, height, pointer=query.get("pointer", ["1"])[0] != "0")
        try:
            for jpeg in frames:
                self.wfile.write(struct.pack(">I", len(jpeg)) + jpeg)
                self.wfile.flush()
        except OSError as exc:
            # The party page stopped reading: Go Live was turned off, or restarted the feed at another size.
            self.close_connection = True
            self.hangup_log.info("A party window's frame feed ended (%s)", describe(exc) if is_hangup(exc) else exc)
        finally:
            frames.close()
