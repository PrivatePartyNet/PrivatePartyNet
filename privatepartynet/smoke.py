"""Checks that the app can serve its pages on this PC. Run it as: PrivatePartyNet.exe --smoke-test

It starts a server on a free port, fetches each page over HTTPS, and returns 0 only when every page comes back
with the text it should contain. Nothing leaves the PC, so it works offline.
"""

import logging
import ssl
import tempfile
import urllib.request

from privatepartynet.cert import ensure_certificate
from privatepartynet.server import Party

log = logging.getLogger("privatepartynet.smoke")

CHECKS = [
    ("/", b"Open this party from the PrivatePartyNet app"),
    ("/static/rtc.js", b"PartyRTC"),
    ("/static/layout.js", b"PrivatePartyNetLayout"),
    ("/static/netstats.js", b"PrivatePartyNetNetStats"),
    ("/static/room.js", b"EventSource"),
    ("/static/style.css", b"statsbar"),
    ("/static/theme.css", b"data-theme"),
]


def run():
    """Return 0 when every check passes, 1 otherwise. Results go to the log."""
    failures = []
    with tempfile.TemporaryDirectory() as folder:
        certfile, keyfile = ensure_certificate(folder, ["127.0.0.1"])
        share = Party("000000", 0, certfile, keyfile, party_name="Smoke test", host_name="Smoke test")
        share.start()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            for path, needle in CHECKS:
                try:
                    with urllib.request.urlopen(f"https://127.0.0.1:{share.port}{path}", context=context, timeout=10) as response:
                        body = response.read()
                except OSError as exc:
                    failures.append(f"{path}: {exc}")
                    continue
                if needle not in body:
                    failures.append(f"{path}: expected text not found")
        finally:
            share.stop()
    if failures:
        for failure in failures:
            log.error("Smoke test failed: %s", failure)
        return 1
    log.info("Smoke test passed: %d pages served over HTTPS", len(CHECKS))
    return 0
