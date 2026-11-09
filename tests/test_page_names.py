"""A page script is one function scope: a second `var` (or function) of the same name there replaces the first. The
3.3 ping display and the reconnect code once both had a `myLink` in room.js, which broke every message on a page that
was being measured; this keeps any such clash from coming back."""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "privatepartynet" / "web"
TOP = re.compile(r"^  (?:var|function) ([A-Za-z_$][\w$]*)", re.M)  # declarations at the top of the page's IIFE


class PageNameTests(unittest.TestCase):
    def test_no_name_is_declared_twice_at_the_top_of_a_page_script(self):
        for name in ("room.js", "control.js", "appshell.js", "chat.js", "voice.js"):
            names = TOP.findall((WEB / name).read_text(encoding="utf-8"))
            twice = sorted({n for n in names if names.count(n) > 1})
            with self.subTest(script=name):
                self.assertEqual(twice, [])


if __name__ == "__main__":
    unittest.main()
