"""Party windows accept exactly the certificate the join checked, per origin, instead of any certificate at all."""

import tempfile
import unittest
from pathlib import Path

from privatepartynet import windowpins
from privatepartynet.cert import ensure_certificate, pins_of_pem


def certificate(folder):
    return Path(ensure_certificate(folder, ["127.0.0.1"])[0]).read_text(encoding="utf-8")


class Certificates(unittest.TestCase):
    """Two certificates, and the pins forgotten after each test."""

    def setUp(self):
        temp, other = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(other.cleanup)
        self.pem, self.other_pem = certificate(temp.name), certificate(other.name)
        self.whole, self.spki = pins_of_pem(self.pem)
        self.addCleanup(windowpins.forget_all)


class PinTests(Certificates):
    def test_origins_are_compared_as_the_browser_sees_them(self):
        self.assertEqual(windowpins.origin("https://192.168.1.95:8765/join?token=x"), "https://192.168.1.95:8765")
        self.assertEqual(windowpins.origin("HTTPS://Host.Example/x"), "https://host.example:443")
        self.assertEqual(windowpins.origin("https://[2603:6000::1]:8765/"), "https://[2603:6000::1]:8765")
        self.assertEqual(windowpins.origin("http://127.0.0.1:5000/host"), "")
        self.assertEqual(windowpins.origin("not a url"), "")

    def test_a_window_takes_the_certificate_the_join_checked_by_either_pin(self):
        for pins in ((self.whole,), (self.spki,), ("", self.spki), [self.whole, self.spki]):
            windowpins.forget_all()
            windowpins.pin("https://192.168.1.95:8765/join?token=x", pins)
            self.assertTrue(windowpins.allowed("https://192.168.1.95:8765/static/room.js", self.pem), pins)

    def test_another_certificate_another_origin_or_no_pin_is_refused(self):
        windowpins.pin("https://192.168.1.95:8765/join?token=x", (self.whole, self.spki))
        self.assertFalse(windowpins.allowed("https://192.168.1.95:8765/", self.other_pem), "someone in between")
        self.assertFalse(windowpins.allowed("https://192.168.1.95:8766/", self.pem), "another port")
        self.assertFalse(windowpins.allowed("https://10.0.0.5:8765/", self.pem), "another address")
        self.assertFalse(windowpins.allowed("https://192.168.1.95:8765/", ""), "no certificate")
        self.assertFalse(windowpins.allowed("https://192.168.1.95:8765/", "garbage"))
        windowpins.pin("https://192.168.1.96:8765/", ("", "not hex"))
        self.assertFalse(windowpins.allowed("https://192.168.1.96:8765/", self.pem), "no usable pin is no pin")

    def test_a_later_join_to_the_same_origin_replaces_its_pin(self):
        # A punched tunnel's local port can be used again by another party.
        windowpins.pin("https://127.0.0.1:50123/join?token=a", (self.whole,))
        windowpins.pin("https://127.0.0.1:50123/join?token=b", pins_of_pem(self.other_pem))
        self.assertFalse(windowpins.allowed("https://127.0.0.1:50123/", self.pem))
        self.assertTrue(windowpins.allowed("https://127.0.0.1:50123/", self.other_pem))


class FakeArgs:
    def __init__(self, uri, pem):
        self.RequestUri = uri
        self.action = None

        class Certificate:
            def ToPemEncoding(self):
                return pem

        self.ServerCertificate = Certificate()

    def set_Action(self, action):
        self.action = action


class FakeWindow:
    def __init__(self):
        self.html = []

    def load_html(self, html):
        self.html.append(html)


class HandlerTests(Certificates):
    """The handler pywebview's WebView2 window calls on a certificate it does not trust."""

    def test_pywebview_still_hooks_the_event_when_told_to_ignore_errors(self):
        """What install() relies on, read from the installed pywebview, so an update that changes it is noticed."""
        import inspect

        from webview.platforms import edgechromium

        source = inspect.getsource(edgechromium.EdgeChrome.on_webview_ready)
        hook = source.split("webview_settings['IGNORE_SSL_ERRORS']:", 1)[1].splitlines()[1]
        self.assertIn("ServerCertificateErrorDetected += self.on_certificate_error", hook)

    def test_install_makes_pywebview_ask_the_pins(self):
        import webview
        from webview.platforms import edgechromium

        original = edgechromium.EdgeChrome.on_certificate_error
        self.addCleanup(setattr, edgechromium.EdgeChrome, "on_certificate_error", original)
        self.addCleanup(webview.settings.__setitem__, "IGNORE_SSL_ERRORS", webview.settings["IGNORE_SSL_ERRORS"])
        windowpins.install()
        self.assertIs(webview.settings["IGNORE_SSL_ERRORS"], True, "so pywebview hooks the event in every window")
        self.assertIs(edgechromium.EdgeChrome.on_certificate_error, windowpins.on_certificate_error)

        actions = edgechromium.CoreWebView2ServerCertificateErrorAction
        browser = type("Browser", (), {"pywebview_window": FakeWindow()})()
        windowpins.pin("https://192.168.1.95:8765/join?token=x", (self.spki,))
        good = FakeArgs("https://192.168.1.95:8765/join?token=x", self.pem)
        windowpins.on_certificate_error(browser, None, good)
        self.assertEqual(good.action, actions.AlwaysAllow)
        bad = FakeArgs("https://192.168.1.95:8765/join?token=x", self.other_pem)
        with self.assertLogs("privatepartynet", "WARNING"):
            windowpins.on_certificate_error(browser, None, bad, delay=0)
        self.assertEqual(bad.action, actions.Cancel)
        (page,) = browser.pywebview_window.html
        self.assertIn("not the one", page)
        self.assertNotIn("BEGIN CERTIFICATE", page)

    def test_in_the_one_window_a_refused_room_is_stopped_in_its_own_place(self):
        """The one window's page (the shell) stays; the shell is told which party's room was refused."""
        from webview.platforms import edgechromium

        window = FakeWindow()
        refused = []
        window.ppn_refused = refused.append
        browser = type("Browser", (), {"pywebview_window": window})()
        windowpins.pin("https://192.168.1.95:8765/join?token=x", (self.spki,))
        bad = FakeArgs("https://192.168.1.95:8765/?embedded=1", self.other_pem)
        with self.assertLogs("privatepartynet", "WARNING"):
            windowpins.on_certificate_error(browser, None, bad, delay=0)
        self.assertEqual(bad.action, edgechromium.CoreWebView2ServerCertificateErrorAction.Cancel)
        self.assertEqual(refused, ["https://192.168.1.95:8765"])
        self.assertEqual(window.html, [], "nothing replaces the window's own page")


if __name__ == "__main__":
    unittest.main()
