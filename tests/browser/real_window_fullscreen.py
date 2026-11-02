"""Fullscreen in the real window stack: pywebview, WinForms and WebView2, which the headless browser test cannot reach.

    py tests\\browser\\real_window_fullscreen.py

Starts a party in this process, opens its host page in a hidden window (nothing appears on screen), goes live from this PC's
own screen so a tile exists, then presses the tile's Fullscreen button and the top bar's Fullscreen button and checks the
window's real rectangle against the display's. The headless browser test can only see the page; a page-level fullscreen
that leaves the window in its frame (as the first version did) passes there and fails here.

With --visible the window is shown (without taking focus) and only the stream's fullscreen is tried, for about three
seconds, because a hidden WebView2 does not resize its page and so cannot show that the page really fills the display.
A screenshot of the display is saved to the folder named after --shot.

Bounded: a watchdog ends the process after 120 seconds whatever happens. Prints one line per check and ends with RESULT.
"""

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from privatepartynet.app import WindowApi, webview_options  # noqa: E402
from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.frames import ScreenGrabber  # noqa: E402
from privatepartynet.server import Party  # noqa: E402

user32 = ctypes.WinDLL("user32")
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


failed = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""), flush=True)
    if not ok:
        failed.append(name)


def rects(window):
    handle = window.native.Handle.ToInt32()
    rect = wintypes.RECT()
    user32.GetWindowRect(handle, ctypes.byref(rect))
    info = MonitorInfo()
    info.size = ctypes.sizeof(MonitorInfo)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(handle, 2), ctypes.byref(info))
    box = lambda r: (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return box(rect), box(info.monitor)


def js(window, script):
    return window.evaluate_js(script)


def wait_for(window, script, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if js(window, script):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


VISIBLE = "--visible" in sys.argv
SHOT = sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else None


def run(window):
    try:
        check("the host page loads in the real window", wait_for(window, "!!document.getElementById('goLive') && !!window.pywebview && !!window.pywebview.api"))
        check("the app offers the window bridge", js(window, "typeof window.pywebview.api.set_fullscreen === 'function'"))
        check("the screens are listed", wait_for(window, "document.querySelectorAll('#monitor option').length > 0"))
        js(window, "document.getElementById('goLive').click(); true")
        check("going live makes a tile", wait_for(window, "document.querySelectorAll('.tile').length > 0 && !!document.querySelector('.tile .tool.fullscreen')"))
        time.sleep(1.5)

        before, display = rects(window)
        check("the window starts smaller than the display", before[2] < display[2] or before[3] < display[3], f"window {before}, display {display}")

        js(window, "document.querySelector('.tile .tool.fullscreen').click(); true")
        time.sleep(1.5)
        if SHOT:
            from PIL import ImageGrab

            ImageGrab.grab(all_screens=True).save(os.path.join(SHOT, "fullscreen_tile.png"))
        full, display = rects(window)
        check("the stream's fullscreen button makes the WINDOW cover the display", full == display, f"window {full}, display {display}")
        page = json.loads(js(window, "JSON.stringify({ w: innerWidth, h: innerHeight, dpr: devicePixelRatio, "
                                     "tile: (function () { var r = document.querySelector('.tile').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })(), "
                                     "top: getComputedStyle(document.getElementById('top')).display, side: getComputedStyle(document.getElementById('side')).display, "
                                     "bar: getComputedStyle(document.getElementById('statsbar')).display })"))
        if VISIBLE or abs(page["w"] - 1000) > 3:  # a hidden window's page is not resized, so only a shown one can be checked
            check("the page fills the display", abs(page["w"] * page["dpr"] - display[2]) < 3 and abs(page["h"] * page["dpr"] - display[3]) < 3, str(page))
        check("the stream fills the page", abs(page["tile"][2] - page["w"]) < 2 and abs(page["tile"][3] - page["h"]) < 2 and page["tile"][0] == 0 and page["tile"][1] == 0)
        check("the top bar, side panel and bottom bar are out of the way", page["top"] == "none" and page["side"] == "none" and page["bar"] == "none")

        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); true")
        time.sleep(1.5)
        back, _ = rects(window)
        check("Escape puts the window back", back == before, f"window {back}, was {before}")
        check("and the bars return", js(window, "getComputedStyle(document.getElementById('top')).display !== 'none'"))
        if VISIBLE:
            return

        js(window, "document.getElementById('fullBtn').click(); true")
        time.sleep(1.5)
        full2, display = rects(window)
        check("the top bar's Fullscreen makes the window cover the display", full2 == display, f"window {full2}, display {display}")
        check("it hides the bottom bar", js(window, "document.getElementById('statsbar').hidden"))
        js(window, "document.getElementById('fullBtn').click(); true")
        time.sleep(1.5)
        back2, _ = rects(window)
        check("pressing it again restores the window", back2 == before, f"window {back2}")
        check("and shows the bottom bar", js(window, "!document.getElementById('statsbar').hidden"))

        js(window, "document.querySelector('.tile .tool.fullscreen').click(); true")
        time.sleep(1.0)
        js(window, "document.getElementById('fullBtn').click(); true")  # window fullscreen on top of a stream fullscreen
        time.sleep(1.0)
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); true")
        time.sleep(1.0)
        still, display = rects(window)
        check("leaving the stream fullscreen keeps the window fullscreen when the top bar asked for it too", still == display, f"window {still}")
    except Exception as err:
        check("the run finished without an error", False, repr(err))
    finally:
        print("RESULT " + ("PASS" if not failed else f"FAIL {len(failed)}"), flush=True)
        window.destroy()


def main():
    folder = tempfile.mkdtemp(prefix="privatepartynet-fullscreen-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("315902", 8797, certfile, keyfile, party_name="Fullscreen check", host_name="Host", grabber=ScreenGrabber())
    party.start()
    origin = f"http://127.0.0.1:{party.local_port}"
    api = WindowApi(origin)
    window = webview.create_window("Fullscreen check", url=origin + "/host", js_api=api, hidden=not VISIBLE, focus=not VISIBLE, width=1000, height=640)
    api._attach(window)
    watchdog = threading.Timer(120, lambda: os._exit(2))
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(run, window, **webview_options(Path(folder) / "webview"))
    finally:
        watchdog.cancel()
        party.stop()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
