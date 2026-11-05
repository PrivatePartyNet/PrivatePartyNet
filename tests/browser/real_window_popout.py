"""Pop-out windows and Leave in the real window stack (pywebview, WinForms, WebView2), which headless Edge cannot reach.

    py tests\\browser\\real_window_popout.py
    py tests\\browser\\real_window_popout.py --pinned   (the windows check the certificate as the app's do)

Starts a party in this process and opens three real windows: the host's, a member's (who shares a drawn screen), and, from the
member's Pop out button, the pop-out window the app itself creates. Checks what the headless test can only fake: the pop-out
shares the app's cookie jar with the window that opened it and still works, the opener can pop out again, and Leave closes
the party window and the pop-out with it. The pop-out window is briefly visible (the app makes it); the others are hidden.

Bounded: a watchdog ends the process after 150 seconds whatever happens.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# --pinned: the member's window and its pop-out accept the party's certificate the way the app's do (windowpins.py),
# instead of through --ignore-certificate-errors.
PINNED = "--pinned" in sys.argv[1:]
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
    ("" if PINNED else "--ignore-certificate-errors ")
    + "--allow-loopback-in-peer-connection --disable-features=WebRtcHideLocalIpsWithMdns"
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from privatepartynet import windowpins  # noqa: E402
from privatepartynet.app import WindowApi, webview_options  # noqa: E402
from privatepartynet.cert import ensure_certificate, pins_of_pem  # noqa: E402
from privatepartynet.server import Party  # noqa: E402

failed = []

FAKE_SCREEN = """(function () {
  navigator.mediaDevices.getDisplayMedia = async function () {
    var canvas = document.createElement("canvas");
    canvas.width = 640; canvas.height = 360;
    var ctx = canvas.getContext("2d"), n = 0;
    setInterval(function () { n += 1; ctx.fillStyle = "hsl(" + (n * 5 % 360) + ", 80%, 50%)"; ctx.fillRect(0, 0, 640, 360); }, 33);
    return canvas.captureStream(30);
  };
})();"""


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""), flush=True)
    if not ok:
        failed.append(name)


def js(window, script):
    return window.evaluate_js(script)


def wait_for(window, script, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if js(window, script):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def run(host_window, party, member_window, member_api, host_api):
    try:
        check("the host's window loads", wait_for(host_window, "!!document.getElementById('goLive')"))
        check("the member's window loads", wait_for(member_window, "!!document.getElementById('goLive') && !!window.pywebview && !!window.pywebview.api"))
        js(member_window, FAKE_SCREEN + "; true")
        seen = wait_for(host_window, "document.getElementById('count').textContent.startsWith('2 people')")
        check("they see each other", seen)
        if not seen:
            probe = ('JSON.stringify({ count: document.getElementById("count").textContent, url: location.href, '
                     'status: document.getElementById("status").textContent, roster: document.getElementById("roster").textContent })')
            for label, w in (("host", host_window), ("member", member_window)):
                print(f"   {label}: {js(w, probe)}", flush=True)
        js(member_window, "document.getElementById('goLive').click(); true")
        check("the host receives the member's screen as a tile", wait_for(host_window, "document.querySelectorAll('.tile').length > 0 && !!document.querySelector('.tile .tool.popout')", 40))
        before = len(webview.windows)

        js(host_window, "document.querySelector('.tile .tool.popout').click(); true")
        check("Pop out opens a real window", wait_for(host_window, "true") and _wait(lambda: len(webview.windows) == before + 1, 20), f"{before} windows before")
        popout = next((w for w in webview.windows if w.title == "PrivatePartyNet screen"), None)
        check("it is the app's pop-out window", popout is not None)
        if popout is None:
            return
        shown = wait_for(popout, "document.body.classList.contains('viewer') && document.querySelectorAll('.tile').length === 1", 40)
        check("the pop-out shows one screen", shown)
        if not shown:
            print("   pop-out page:", js(popout, 'JSON.stringify({ url: location.href, cls: document.body.className, tiles: document.querySelectorAll(".tile").length, status: document.getElementById("status").textContent, me: document.getElementById("roster").textContent })'), flush=True)
        video = "JSON.stringify((function () { var v = document.querySelector('.tile video'); if (!v) return { video: false }; "                 "var tracks = v.srcObject ? v.srcObject.getTracks().map(function (t) { return t.readyState + '/' + t.muted; }) : []; "                 "return { w: v.videoWidth, ready: v.readyState, paused: v.paused, tracks: tracks, chips: document.getElementById('peerChips').textContent, hidden: document.hidden }; })())"
        got = wait_for(popout, "(function () { var v = document.querySelector('.tile video'); return v && v.videoWidth > 0; })()", 40)
        check("the pop-out receives live video", got)
        if not got:
            print("   pop-out video:", js(popout, video), flush=True)
            print("   host tile video:", js(host_window, video), flush=True)
        check("the pop-out has settings and fullscreen but no pop out", js(popout, "document.querySelectorAll('.tile .tools .tool').length === 2 && !document.querySelector('.tile .tool.popout')"))
        check("the party does not count the pop-out as a person", js(host_window, "document.getElementById('count').textContent.startsWith('2 people')"))

        # the bug that showed "that screen is not live": the pop-out joined under the opener's own cookie
        js(host_window, "window.__popout = null; fetch('/popout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: document.querySelector('.tile').dataset.member, slot: Number(document.querySelector('.tile').dataset.slot) }) }).then(function (r) { return r.text().then(function (text) { window.__popout = r.status + ' ' + text.slice(0, 80); }); }); true")
        wait_for(host_window, "window.__popout !== null", 15)
        status = js(host_window, "window.__popout")
        check("the host's window can still pop out a second time", str(status).startswith("200"), f"{status}")
        if not str(status).startswith("200"):
            print("   server roster:", [(m["name"], m["role"], m["online"]) for m in party.roster()], flush=True)
            print("   host window cookies visible to the page:", js(host_window, "document.cookie"), flush=True)
            print("   host token known to the server:", party.member_for(party.host_token) is not None, flush=True)
        check("the host's window still has the party", js(host_window, "!document.getElementById('top').hidden && document.getElementById('count').textContent.startsWith('2 people')"))

        js(popout, "document.querySelector('.tile .tool.fullscreen').click(); true")
        time.sleep(1.2)
        check("fullscreen in the pop-out makes its window cover the display", js(popout, "document.querySelector('.tile').classList.contains('in-window-fullscreen')"))
        js(popout, "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); true")
        time.sleep(0.8)

        # Leave in the host's window closes it and the pop-out the app made from it
        count = len(webview.windows)
        js(host_window, "document.getElementById('leaveBtn').click(); true")
        check("Leave closes the party window and its pop-out", _wait(lambda: popout not in webview.windows and host_window not in webview.windows, 25),
              f"{count} windows, {len(webview.windows)} left")
        check("the member's window is still there", member_window in webview.windows)
    except Exception as err:
        check("the run finished without an error", False, repr(err))
    finally:
        print("RESULT " + ("PASS" if not failed else f"FAIL {len(failed)}"), flush=True)
        for window in list(webview.windows):
            try:
                window.destroy()
            except Exception:
                pass


def _wait(test, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.3)
    return False


def main():
    folder = tempfile.mkdtemp(prefix="privatepartynet-popout-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("315902", 8796, certfile, keyfile, party_name="Pop-out check", host_name="Host")
    party.start()
    local = f"http://127.0.0.1:{party.local_port}"
    host_api = WindowApi(local)
    host_window = webview.create_window("Host check", url=local + "/host", js_api=host_api, hidden=True, width=1100, height=700)
    host_api._attach(host_window)
    # a different host name, so the member does not share the host window's cookies (cookies ignore the port)
    member_origin = f"https://localhost:{party.port}"
    if PINNED:
        windowpins.install()
        windowpins.pin(member_origin, pins_of_pem(Path(certfile).read_text(encoding="utf-8")))
    member_api = WindowApi(member_origin)
    token = party.join_token("Alex")
    member_window = webview.create_window("Member check", url=f"{member_origin}/join?token={token}", js_api=member_api, hidden=True, width=1100, height=700)
    member_api._attach(member_window)
    watchdog = threading.Timer(150, lambda: os._exit(2))
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(lambda: run(host_window, party, member_window, member_api, host_api), **webview_options(Path(folder) / "webview"))
    finally:
        watchdog.cancel()
        party.stop()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
