"""Checks that the app can serve its pages on this PC. Run it as: PrivatePartyNet.exe --smoke-test

It starts a server on a free port, fetches each page over HTTPS, and returns 0 only when every page comes back
with the text it should contain. Nothing leaves the PC, so it works offline.
"""

import logging
import ssl
import struct
import sys
import tempfile
import urllib.request

from privatepartynet.cert import ensure_certificate
from privatepartynet.paths import asset_path
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


BUNDLED = ("PrivatePartyNetGameNet.exe", "wintun.dll")  # the game network: its helper, and the adapter driver it loads
AMD64 = 0x8664


def machine(path):
    """The CPU a Windows program or DLL is built for (0x8664 is 64-bit x86), or None when it is not one."""
    try:
        with open(path, "rb") as file:
            header = file.read(4096)
    except OSError:
        return None
    if header[:2] != b"MZ" or len(header) < 64:
        return None
    offset = struct.unpack_from("<I", header, 0x3C)[0]
    if offset + 6 > len(header) or header[offset:offset + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", header, offset + 4)[0]


def bundled_problems(find=asset_path):
    """What is wrong with the game network's files in the package: missing, or not 64-bit x86. [] when all is well."""
    problems = []
    for name in BUNDLED:
        path = find(name)
        if not path.is_file():
            problems.append(f"{name} is missing ({path})")
        elif machine(path) != AMD64:
            problems.append(f"{name} is not a 64-bit x86 Windows program")
    return problems


def run():
    """Return 0 when every check passes, 1 otherwise. Results go to the log. The packaged app also checks that the game
    network's helper and wintun.dll came with it (a checkout has the helper only once it is built)."""
    failures = list(bundled_problems()) if getattr(sys, "frozen", False) else []
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
