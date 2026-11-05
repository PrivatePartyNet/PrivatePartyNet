"""The direct messages page, run in headless Edge against a stand-in for the Python bridge.

Edge loads a copy of dm.html with a few lines added before dm.js: a fake window.pywebview.api that answers like the
app would. The page's own script then draws the people, the conversation and the pictures, and the finished page is
dumped and read. Skipped when Edge is not installed."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from privatepartynet.paths import web_dir

EDGES = [
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
]
EDGE = next((p for p in EDGES if p.exists()), None)

STUB = """
<script>
window.__sent = [];
window.pywebview = { api: {
  about_info: function () { return Promise.resolve({ theme: "dark", name: "PrivatePartyNet" }); },
  dm_state: function () { return Promise.resolve({ ok: true, me: "me", dm_ready: true,
    contacts: [
      { id: "ann", name: "Ann <b>Bold</b>", online: true, unread: 2, preview: "hi", last: 20, new: true, waiting: 0 },
      { id: "ben", name: "Ben", online: false, unread: 0, preview: "", last: 10, new: false, waiting: 1 } ],
    nearby: [ { id: "cy", name: "Cy", address: "10.0.0.9" } ] }); },
  dm_history: function (id) { return Promise.resolve({ ok: true, messages: [
    { id: "1", mine: false, text: "hello <img src=x onerror=alert(1)>", name: "Ann", ts: 1000, pending: false, image: null },
    { id: "2", mine: true, text: "hi Ann", name: "Me", ts: 1010, pending: true, image: null },
    { id: "3", mine: false, text: "", name: "Ann", ts: 1020, pending: false, image: "abc" } ] }); },
  dm_read: function () { return Promise.resolve({ ok: true }); },
  dm_send: function (id, text, img) { window.__sent.push([id, text, img]); return Promise.resolve({ ok: true, message: "" }); },
  dm_add: function () { return Promise.resolve({ ok: true, message: "" }); },
  dm_image: function () { return Promise.resolve({ ok: true, src: "data:image/gif;base64,R0lGODlhAQABAAAAACw=" }); },
  dm_safety: function () { return Promise.resolve({ ok: true, number: "11111 22222" }); },
  saved_servers: function () { return Promise.resolve([{ server: "abcdef0123456789", name: "Heroes night",
    channels: [{ id: "general", name: "general" }] }]); },
  saved_history: function () { return Promise.resolve({ ok: true, messages: [] }); }
} };
</script>
"""


@unittest.skipUnless(EDGE, "Microsoft Edge is not installed")
class DirectMessagesPageTests(unittest.TestCase):
    def dump(self, after_load=""):
        folder = tempfile.mkdtemp(prefix="privatepartynet-dm-page-")
        self.addCleanup(shutil.rmtree, folder, True)
        web = web_dir()
        html = (web / "dm.html").read_text(encoding="utf-8")
        for name in ("theme.css", "ui.css", "dm.css", "icons.js", "panels.js", "chat.js", "dm.js"):
            html = html.replace(f'"{name}"', f'"{(web / name).as_uri()}"')
        html = html.replace('<script src="' + (web / "chat.js").as_uri(), STUB + '<script src="' + (web / "chat.js").as_uri(), 1)
        html = html.replace("</body>", after_load + "</body>")
        page = Path(folder) / "dm_test.html"
        page.write_text(html, encoding="utf-8")
        profile = Path(folder) / "profile"
        run = subprocess.run(
            [str(EDGE), "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
             f"--user-data-dir={profile}", "--virtual-time-budget=4000", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(run.returncode, 0, run.stderr[-500:])
        return run.stdout

    def test_people_nearby_and_a_conversation_are_drawn_safely(self):
        # open Ann's conversation by clicking her, then let the page poll once more
        dom = self.dump('<script>setTimeout(function () { document.querySelector("li.contact").click(); }, 300);</script>')
        self.assertIn("Ann &lt;b&gt;Bold&lt;/b&gt;", dom, "a name is text, never markup")
        self.assertNotIn('class="name"><b>', dom)
        self.assertIn(">Ben<", dom)
        self.assertIn(">Cy<", dom)
        self.assertRegex(dom, r'class="unread">2<')
        self.assertIn(">New<", dom)
        self.assertIn("hello &lt;img src=x onerror=alert(1)&gt;", dom, "message text is text too")
        self.assertNotIn('class="text">hello <img', dom)
        self.assertIn("sending...", dom)
        self.assertEqual(len(re.findall(r'class="dm-image"', dom)), 1)
        self.assertIn("data:image/gif;base64", dom)
        self.assertIn("on the network now", dom)
        self.assertIn("Heroes night  #general", dom, "saved servers are listed for reading offline")

    def test_the_composer_sends_text_through_the_bridge(self):
        script = ('<script>setTimeout(function () { document.querySelector("li.contact").click(); '
                  'setTimeout(function () { document.getElementById("text").value = "typed"; '
                  'document.getElementById("composer").requestSubmit(); '
                  'document.body.setAttribute("data-sent", JSON.stringify(window.__sent)); }, 400); }, 300);</script>')
        dom = self.dump(script)
        self.assertIn('data-sent="[[&quot;ann&quot;,&quot;typed&quot;,null]]"', dom)


    def test_a_picture_dropped_anywhere_on_the_window_is_sent(self):
        """There is no picture button: a picture dragged onto the window goes to the open conversation, and a file that is
        not a picture is refused with a note."""
        # Encoding is covered by the party e2e test and tests/js; here it is stood in for, because headless Edge's virtual
        # clock does not wait for work done off the page, and this test is about where a drop goes.
        script = ('<script>PartyChat.preparePicture = function (file) { return Promise.resolve({ blob: file, w: 40, h: 30 }); }; '
                  'setTimeout(function () { document.querySelector("li.contact").click(); '
                  'setTimeout(function () { var bytes = new Uint8Array(64); bytes.set([137, 80, 78, 71, 13, 10, 26, 10]); '
                  'var data = new DataTransfer(); data.items.add(new File([bytes], "p.png", { type: "image/png" })); '
                  'data.items.add(new File(["x"], "x.txt", { type: "text/plain" })); '
                  'var drop = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: data }); '
                  'document.getElementById("pick").dispatchEvent(drop); '
                  'document.body.setAttribute("data-taken", String(drop.defaultPrevented)); }, 400); }, 300); '
                  # record each send as it happens: the picture is encoded off the page's clock, so a timer could look too early
                  'var send = window.pywebview.api.dm_send; window.pywebview.api.dm_send = function () { '
                  'var answer = send.apply(this, arguments); '
                  'document.body.setAttribute("data-sent", JSON.stringify(window.__sent.map(function (s) { '
                  'return [s[0], s[1], typeof s[2] === "string" && s[2].length > 20]; }))); return answer; };</script>')
        dom = self.dump(script)
        self.assertIn('data-taken="true"', dom, "the window takes the drop instead of opening the file")
        self.assertIn('data-sent="[[&quot;ann&quot;,&quot;&quot;,true]]"', dom, "one picture went to Ann")
        self.assertIn("One file was not sent", dom)


if __name__ == "__main__":
    unittest.main()
