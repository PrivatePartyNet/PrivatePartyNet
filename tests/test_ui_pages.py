"""The pages and the scripts that drive them agree.

Each window's script finds its elements by id. The 3.2.0 interface rebuilt every page's markup and kept every id, so these
tests fail the moment a page loses an element its script reaches for, an icon is named that the icon set does not draw, or a
stylesheet uses a color or size that theme.css never defines."""

import re
import unittest

from privatepartynet.paths import web_dir

WEB = web_dir()
# page -> the scripts it loads that look elements up by id
PAGES = {
    "control.html": ("control.js", "nav.js"),
    "room.html": ("room.js", "shell.js"),
    "dm.html": ("dm.js",),
    "about.html": ("info.js",),
}
ID_LOOKUP = re.compile(r"""getElementById\(\s*["']([A-Za-z0-9_-]+)["']\s*\)""")
# ids a script asks for only to check whether the page has them (one script serves pages that differ)
OPTIONAL = {"info.js": {"facts", "copy", "copied"}, "nav.js": {"ppnToast", "pages"}, "control.js": set()}


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


class PageScriptTests(unittest.TestCase):
    def test_every_id_a_script_looks_up_is_on_its_page(self):
        for page, scripts in PAGES.items():
            ids = set(re.findall(r'\bid="([^"]+)"', read(page)))
            for script in scripts:
                wanted = set(ID_LOOKUP.findall(read(script))) - OPTIONAL.get(script, set())
                missing = sorted(wanted - ids)
                with self.subTest(page=page, script=script):
                    self.assertEqual(missing, [], f"{page} lacks elements {script} looks up")

    def test_no_id_is_used_twice_on_a_page(self):
        for page in list(PAGES) + ["how.html", "login.html"]:
            ids = re.findall(r'\bid="([^"]+)"', read(page))
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            with self.subTest(page=page):
                self.assertEqual(dupes, [])

    def test_every_page_loads_the_shared_look(self):
        for page in list(PAGES) + ["how.html", "login.html"]:
            text = read(page)
            with self.subTest(page=page):
                self.assertRegex(text, r'href="(/static/)?theme\.css"')
                self.assertRegex(text, r'href="(/static/)?ui\.css"')
                self.assertLess(text.index("theme.css"), text.index("ui.css"), "the colors load before the components")

    def test_the_party_server_serves_every_file_the_room_loads(self):
        from privatepartynet.server import STATIC_TYPES

        for page in ("room.html", "login.html"):
            for name in re.findall(r'(?:src|href)="/static/([^"]+)"', read(page)):
                with self.subTest(page=page, file=name):
                    self.assertIn(name, STATIC_TYPES, f"{page} loads /static/{name}, which the party does not serve")
                    self.assertTrue((WEB / name).is_file())


class IconTests(unittest.TestCase):
    def test_every_icon_a_page_uses_is_drawn(self):
        source = read("icons.js")
        drawn = set(re.findall(r'^\s+"?([a-z-]+)"?: \'', source, re.M))
        self.assertGreater(len(drawn), 30)
        used = set()
        for path in WEB.iterdir():
            if path.suffix in (".html", ".js") and path.name != "icons.js":
                text = path.read_text(encoding="utf-8")
                used |= set(re.findall(r'href="#i-([a-z-]+)"', text))
                used |= set(re.findall(r'data-icon="([a-z-]+)"', text))
                for call in re.findall(r"PPNIcons\.node\(([^)]*)\)", text):
                    used |= set(re.findall(r'"([a-z-]+)"', call))
        self.assertEqual(sorted(used - drawn), [])

    def test_icons_are_fixed_drawings_with_nothing_from_outside(self):
        source = read("icons.js")
        self.assertNotIn("http://", source.replace('xmlns="http://www.w3.org/2000/svg"', ""))
        self.assertNotIn("https://", source)


class ThemeTests(unittest.TestCase):
    def test_every_variable_the_stylesheets_use_is_defined(self):
        theme = read("theme.css")
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", theme))
        for sheet in ("ui.css", "control.css", "style.css", "dm.css", "info.css", "theme.css"):
            # a variable used with a fallback, var(--x, 252px), may be left undefined: the panel widths are set at run time
            # by panels.js when someone drags an edge
            used = set(re.findall(r"var\((--[a-z0-9-]+)\)", read(sheet)))
            with self.subTest(sheet=sheet):
                self.assertEqual(sorted(used - defined), [])

    def test_the_three_appearance_choices_exist(self):
        theme = read("theme.css")
        for choice in ("light", "dark", "system"):
            self.assertIn(f'[data-theme="{choice}"]', theme)

    def test_text_contrast_is_comfortable_in_both_themes(self):
        """Body text at least 7:1 and at most 16:1 on the page and panel colors, secondary text at least 4.5:1, white on
        every filled button at least 4.5:1. Discord users with astigmatism complained of near-white text on near-black."""
        theme = read("theme.css")
        colors = dict(re.findall(r"(--[a-z0-9-]+):\s*light-dark\((#[0-9a-f]{6}),\s*#[0-9a-f]{6}\)", theme))
        darks = dict(re.findall(r"(--[a-z0-9-]+):\s*light-dark\(#[0-9a-f]{6},\s*(#[0-9a-f]{6})\)", theme))

        def lum(hexcolor):
            r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        for name, palette in (("light", colors), ("dark", darks)):
            for background in ("--bg", "--surface", "--surface-2"):
                with self.subTest(theme=name, background=background):
                    self.assertGreaterEqual(ratio(palette["--text"], palette[background]), 7)
                    self.assertLessEqual(ratio(palette["--text"], palette[background]), 16)
                    self.assertGreaterEqual(ratio(palette["--muted"], palette[background]), 4.5)
            for fill in ("--accent", "--success-fill", "--danger-fill"):
                with self.subTest(theme=name, fill=fill):
                    self.assertGreaterEqual(ratio("#ffffff", palette[fill]), 4.5)


class ComposerTests(unittest.TestCase):
    def test_the_message_boxes_have_only_send(self):
        """No gift, sticker, app or other buttons in the message box (a long-running Discord complaint). Pictures are
        dragged onto the window or pasted (K, 2026-11-04: no picture button)."""
        for page, form in (("room.html", "chatForm"), ("dm.html", "composer")):
            text = read(page)
            block = re.search(rf'<form id="{form}"[^>]*>(.*?)</form>', text, re.S).group(1)
            buttons = re.findall(r"<button\b[^>]*>", block)
            with self.subTest(page=page):
                self.assertEqual(len(buttons), 1, buttons)

    def test_a_voice_channel_is_joined_with_its_button_only(self):
        chat = read("chat.js")
        self.assertIn('joinButton.classList.add("join")', chat)
        # only text channels get a click handler on the row itself
        self.assertRegex(chat, r'if \(!isVoice\) \{\s*row\.addEventListener\("click"')


if __name__ == "__main__":
    unittest.main()
