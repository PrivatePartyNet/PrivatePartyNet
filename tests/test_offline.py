"""Offline guarantees: the pages load nothing from the internet, and the call uses no STUN or TURN server."""

import re
import unittest

from privatepartynet.paths import asset_path, web_dir

URL = re.compile(r"""(?:src|href|url\()\s*=?\s*["']?(https?:)?//([^"'\s)>]+)""", re.IGNORECASE)
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class OfflineTests(unittest.TestCase):
    def test_pages_and_scripts_reference_no_remote_hosts(self):
        offenders = []
        for path in web_dir().iterdir():
            if path.suffix not in {".html", ".js", ".css"}:
                continue
            for match in URL.finditer(path.read_text(encoding="utf-8")):
                host = match.group(2).split("/")[0].split(":")[0]
                if host not in ALLOWED_HOSTS:
                    offenders.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_the_call_uses_no_relay_and_stun_only_once_someone_joined_from_outside(self):
        """On one network nothing outside it is asked. STUN (which only tells a PC its public address) is switched on
        by the party once someone joins from outside the network, and there is never a TURN server: nothing relays."""
        source = (web_dir() / "rtc.js").read_text(encoding="utf-8")
        self.assertIn("var CONFIG = { iceServers: [] };", source, "the default is no ICE server at all")
        self.assertNotIn("turn:", source)
        self.assertNotIn("turns:", source)
        room = (web_dir() / "room.js").read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r"RTC\.setStun\(([^)]*)\)", room))), ["!!msg.stun"],
                         "STUN follows only what the party says")

    def test_the_pages_the_app_needs_are_bundled(self):
        for name in ("login.html", "room.html", "rtc.js", "room.js", "style.css"):
            self.assertTrue((web_dir() / name).is_file(), name)
        self.assertTrue(asset_path("privatepartynet.ico").is_file())


if __name__ == "__main__":
    unittest.main()
