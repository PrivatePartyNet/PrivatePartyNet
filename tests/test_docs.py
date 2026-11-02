import re
import unittest
from pathlib import Path

from privatepartynet import APP_NAME, __version__

WEB = Path(__file__).resolve().parents[1] / "privatepartynet" / "web"
PAGES = ("how.html", "about.html", "control.html")


class HowPageTests(unittest.TestCase):
    """The How page is part of every release. This fails the build when the version moves and the page does not."""

    def setUp(self):
        self.text = (WEB / "how.html").read_text(encoding="utf-8")

    def test_the_how_page_was_reviewed_for_this_version(self):
        meta = re.search(r'<meta name="reviewed-for-version" content="([^"]+)"', self.text)
        self.assertIsNotNone(meta, "how.html needs a reviewed-for-version meta tag")
        self.assertEqual(
            meta.group(1), __version__,
            f"Update web/how.html for version {__version__} (check every section against what changed), then set "
            f"reviewed-for-version and the stamp at the bottom to {__version__}.",
        )

    def test_the_visible_stamp_matches_the_meta_tag(self):
        stamp = re.search(r'<span class="appversion">([^<]+)</span>', self.text)
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.group(1), __version__)

    def test_the_pages_use_the_one_app_name(self):
        for page in PAGES:
            text = (WEB / page).read_text(encoding="utf-8")
            for node in re.findall(r'class="appname">([^<]*)<', text):
                self.assertEqual(node, APP_NAME, f"{page} shows a different app name")

    def test_the_user_facing_pages_follow_the_writing_rules(self):
        for page in PAGES:
            text = (WEB / page).read_text(encoding="utf-8")
            self.assertNotIn("\u2014", text, f"{page} has an em dash")
            self.assertNotIn("\u2013", text, f"{page} has an en dash")
            self.assertNotRegex(text, r"(?i)\bsits within\b|\bturns on\b|\bdeep dive\b|\bseamless\b|\brobust\b")


if __name__ == "__main__":
    unittest.main()
