"""The icon: the party-hat monitor drawn by tools\\make_icon.py, and an installer that makes Windows show it.

Windows keeps a cache of icons by file path, so a copy of PrivatePartyNet that is already installed (or pinned to the taskbar) can
go on showing an old icon after an update. The installer therefore ships the .ico as its own file, points the shortcuts at
it, and tells Explorer that icons changed."""

import hashlib
import re
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "privatepartynet.ico"
# The monitor with a party hat and confetti. Drawing a new icon means updating this fingerprint on purpose.
ICON_SHA256 = "0746df4761abe530cd323b6bf49ea60bdf32e4b714cc99692d32c94b62aac67a"


def installer_text():
    return (ROOT / "installer" / "PrivatePartyNet.iss").read_text(encoding="utf-8-sig")


class IconTests(unittest.TestCase):
    def test_the_icon_is_the_party_hat_monitor(self):
        self.assertEqual(hashlib.sha256(ICON.read_bytes()).hexdigest(), ICON_SHA256)

    def test_the_icon_has_every_size_windows_asks_for(self):
        with Image.open(ICON) as image:
            sizes = set(image.info["sizes"])
        for side in (16, 24, 32, 48, 64, 256):
            self.assertIn((side, side), sizes)

    def test_the_icon_is_not_blank_at_the_small_sizes(self):
        with Image.open(ICON) as image:
            image.size = (16, 16)
            alpha = image.convert("RGBA").getchannel("A").tobytes()
        self.assertGreater(sum(1 for value in alpha if value > 200), 100)

    def test_the_package_and_the_installer_use_this_icon(self):
        self.assertIn('"--icon", ROOT / "assets" / "privatepartynet.ico"', (ROOT / "tools" / "build.py").read_text(encoding="utf-8"))
        self.assertIn("SetupIconFile=..\\assets\\privatepartynet.ico", installer_text())

    def test_the_installer_tells_explorer_the_icons_changed(self):
        text = installer_text()
        self.assertRegex(text, r"(?m)^ChangesAssociations=yes$")
        self.assertIn("ie4uinit.exe", text)

    def test_the_icon_is_installed_as_a_file_of_its_own(self):
        text = installer_text()
        self.assertIn('Source: "..\\assets\\privatepartynet.ico"; DestDir: "{app}"', text)
        self.assertIn("UninstallDisplayIcon={app}\\privatepartynet.ico", text)

    def test_every_shortcut_to_the_program_names_the_icon(self):
        text = installer_text()
        icons = text.split("[Icons]", 1)[1].split("[Run]", 1)[0]
        shortcuts = [line for line in icons.splitlines() if line.startswith("Name:") and "{#AppExe}" in line]
        self.assertGreaterEqual(len(shortcuts), 3)
        for line in shortcuts:
            self.assertRegex(line, re.escape('IconFilename: "{app}\\privatepartynet.ico"'), line)


if __name__ == "__main__":
    unittest.main()
