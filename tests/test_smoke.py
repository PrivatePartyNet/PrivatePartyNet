import tempfile
import unittest
from pathlib import Path

from privatepartynet.paths import asset_path
from privatepartynet.smoke import AMD64, bundled_problems, machine, run


class SmokeTests(unittest.TestCase):
    def test_every_page_is_served_over_https(self):
        self.assertEqual(run(), 0)

    def test_the_bundled_wintun_is_64_bit(self):
        self.assertEqual(machine(asset_path("wintun.dll")), AMD64)

    def test_missing_or_wrong_game_network_files_are_named(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "wintun.dll").write_bytes(b"not a program")
            problems = bundled_problems(lambda name: Path(folder) / name)
        self.assertEqual(len(problems), 2)
        self.assertIn("PrivatePartyNetGameNet.exe is missing", problems[0])
        self.assertIn("wintun.dll is not a 64-bit", problems[1])


if __name__ == "__main__":
    unittest.main()
