"""Screenshots of the party window in the real window stack, to look at: tile controls, a tile's settings panel, the side
panel hidden. A visible window is placed on monitor 3 (the one to the right of the main screen) for a few seconds.

    py tests\\browser\\real_window_screens.py <folder for the pictures>

Bounded: a watchdog ends the process after 120 seconds.
"""

import ctypes
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
    "--ignore-certificate-errors --allow-loopback-in-peer-connection --disable-features=WebRtcHideLocalIpsWithMdns"
)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from privatepartynet.app import WindowApi, webview_options  # noqa: E402
from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.frames import ScreenGrabber, list_monitors  # noqa: E402
from privatepartynet.server import Party  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
user32 = ctypes.WinDLL("user32")


def js(w, script):
    return w.evaluate_js(script)


def wait_for(w, script, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if js(w, script):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def shot(window, name):
    rect = wintypes.RECT()
    user32.GetWindowRect(window.native.Handle.ToInt32(), ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(OUT / f"{name}.png")
    print("saved", name, flush=True)


def run(host, member):
    try:
        wait_for(host, "!!document.getElementById('goLive') && !!window.pywebview")
        wait_for(member, "!!document.getElementById('goLive')")
        wait_for(host, "document.getElementById('count').textContent.startsWith('2 people')")
        wait_for(host, "document.querySelectorAll('#monitor option').length > 1")
        js(host, "document.getElementById('goLive').click(); true")
        wait_for(host, "document.querySelectorAll('.tile').length > 0")
        js(member, "document.getElementById('myName').value = 'Sam'; document.getElementById('myName').dispatchEvent(new Event('change')); true")
        time.sleep(3)
        js(host, "document.querySelectorAll('.tools').forEach(t => { t.style.opacity = 1; }); true")
        time.sleep(0.5)
        shot(host, "1_tile_controls")
        js(host, "document.querySelector('.tile .tool.settings').click(); true")
        time.sleep(0.6)
        shot(host, "2_tile_settings")
        js(host, "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); document.getElementById('sideBtn').click(); true")
        time.sleep(0.8)
        shot(host, "3_side_panel_hidden")
        js(host, "document.getElementById('sideBtn').click(); document.getElementById('viewBtn').click(); true")
        time.sleep(0.6)
        shot(host, "4_view_panel")
    finally:
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass


def main():
    folder = tempfile.mkdtemp(prefix="privatepartynet-screens-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("315902", 8794, certfile, keyfile, party_name="Screens check", host_name="Host", grabber=ScreenGrabber())
    party.start()
    local = f"http://127.0.0.1:{party.local_port}"
    right = max(list_monitors(), key=lambda m: m["left"])
    host_api = WindowApi(local)
    host = webview.create_window("Screens host", url=local + "/host", js_api=host_api, focus=False, width=1180, height=720,
                                 x=right["left"] + 60, y=60)
    host_api._attach(host)
    member_origin = f"https://localhost:{party.port}"
    member_api = WindowApi(member_origin)
    member = webview.create_window("Screens member", url=f"{member_origin}/join?token={party.join_token('Sam')}", js_api=member_api,
                                   hidden=True, width=900, height=600)
    member_api._attach(member)
    watchdog = threading.Timer(120, lambda: os._exit(2))
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(lambda: run(host, member), **webview_options(Path(folder) / "webview"))
    finally:
        watchdog.cancel()
        party.stop()


if __name__ == "__main__":
    main()
