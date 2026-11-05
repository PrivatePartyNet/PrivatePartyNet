"""The app's own pages, served to its one window on the loopback address.

The main window shows shell.html: the server rail and, in frames, the control panel, direct messages and each server's
party room. The pages are served over http://127.0.0.1 rather than opened as files, for two reasons. The host's own
room is also on 127.0.0.1, so it is the same site as the shell and keeps its strict sign-in cookie inside a frame. And
the shell and its own pages share one origin, so a page in a frame can use the app's bridge (window.top.pywebview).

Only files of the web folder with a known type are served, only to this PC, only by name (no folders), and nothing here
changes anything. A page from anywhere else may not put these pages in a frame (frame-ancestors 'self').
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from privatepartynet.paths import web_dir

log = logging.getLogger("privatepartynet.shell")

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}


class ShellServer:
    """Serves web_dir() on http://127.0.0.1:<port>. start() picks a free port; stop() ends it."""

    def __init__(self, folder=None):
        self.folder = Path(folder or web_dir()).resolve()
        self._server = None
        self._thread = None

    @property
    def port(self):
        return self._server.server_address[1] if self._server else None

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def url(self, page="shell.html"):
        return f"{self.origin}/{page}"

    def start(self):
        folder = self.folder

        class Handler(_Handler):
            pass

        Handler.folder = folder
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="shell-pages", daemon=True)
        self._thread.start()
        log.info("The app's pages are served on %s", self.origin)
        return self

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "PrivatePartyNet"
    timeout = 30
    folder = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        own = f"127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Host") != own:
            self._plain(421, b"Wrong host.")  # a web page renamed to this address (DNS rebinding) is refused
            return
        name = unquote(urlsplit(self.path).path).lstrip("/") or "shell.html"
        path = self._file(name)
        if path is None:
            self._plain(404, b"Not found.")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[path.suffix.lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if path.suffix.lower() == ".html":
            self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        """The web folder's file called `name`, or None: no folder parts, no hidden files, only the known types."""
        if not name or "/" in name or "\\" in name or ":" in name or name.startswith(".") or "\x00" in name:
            return None
        path = (self.folder / name)
        if path.suffix.lower() not in TYPES:
            return None
        try:
            path = path.resolve()
        except OSError:
            return None
        if path.parent != self.folder or not path.is_file():
            return None
        return path

    def do_POST(self):
        self._plain(405, b"Only pages are served here.")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _plain(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
