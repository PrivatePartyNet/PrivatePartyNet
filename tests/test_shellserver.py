"""The one window's own pages: served from the web folder to this PC only, by name, with known types."""

import http.client
import tempfile
import unittest
from pathlib import Path

from privatepartynet.shellserver import ShellServer


class ShellServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "shell.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
        (self.web / "app.js").write_text("var x = 1;", encoding="utf-8")
        (self.web / "secret.py").write_text("KEY = 1", encoding="utf-8")
        (self.web / ".hidden.js").write_text("hidden", encoding="utf-8")
        (self.root / "outside.js").write_text("outside", encoding="utf-8")
        (self.web / "sub").mkdir()
        (self.web / "sub" / "inner.js").write_text("inner", encoding="utf-8")
        self.server = ShellServer(self.web).start()
        self.addCleanup(self.server.stop)

    def get(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.server.port}")
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        response = conn.getresponse()
        return response, response.read()

    def test_pages_and_scripts_are_served_with_their_types(self):
        response, body = self.get("/shell.html")
        self.assertEqual((response.status, body), (200, b"<!doctype html><title>shell</title>"))
        self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(response.getheader("Content-Security-Policy"), "frame-ancestors 'self'")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        response, body = self.get("/app.js?v=2")
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "application/javascript; charset=utf-8"))
        self.assertEqual(self.get("/")[1], b"<!doctype html><title>shell</title>", "the root is the shell")

    def test_nothing_outside_the_list_is_served(self):
        for path in ("/secret.py", "/.hidden.js", "/../outside.js", "/%2e%2e/outside.js", "/..%5coutside.js", "/sub/inner.js",
                     "/sub%2finner.js", "/missing.js", "/C:%5cWindows%5cwin.ini", "/shell.html%00.js"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0].status, 404)

    def test_only_this_address_is_answered(self):
        self.assertEqual(self.get("/shell.html", host="evil.example:80")[0].status, 421)
        self.assertEqual(self.get("/shell.html", host=f"localhost:{self.server.port}")[0].status, 421)

    def test_nothing_can_be_sent_to_it(self):
        self.assertEqual(self.get("/shell.html", method="POST")[0].status, 405)

    def test_sounds_are_served_as_audio(self):
        (self.web / "sound-join.ogg").write_bytes(b"OggS")
        response, body = self.get("/sound-join.ogg")
        self.assertEqual((response.status, response.getheader("Content-Type"), body), (200, "audio/ogg", b"OggS"))

    def test_the_real_web_folder_has_the_shell(self):
        real = ShellServer().start()
        self.addCleanup(real.stop)
        self.assertTrue((real.folder / "shell.html").is_file(), "web/shell.html is the one window's page")
        self.assertTrue(real.url().startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
