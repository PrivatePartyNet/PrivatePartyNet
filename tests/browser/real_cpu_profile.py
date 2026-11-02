"""Where does PrivatePartyNet's CPU go while a screen is shared and received? Measured in the real window stack.

    py tests\\browser\\real_cpu_profile.py [--monitor 2] [--seconds 20] [--codec h264] [--visible] [--fps 30]

Starts a party in this process, opens a host window and a member window (hidden unless --visible), shares one real monitor
from the host through the real frame feed and WebRTC, and lets the member receive it. After a warm-up it measures, for
the whole process tree this script started, the processor time of each kind of process as a share of the whole PC:
  python   this process: the party server, the frame feed, screen capture and JPEG encoding
  renderer the pages (the host's decodes the JPEGs and encodes the video; the member's decodes the video)
  gpu      the browser's GPU process
  browser  the browser's main process
  other    utility and crash handler processes
It also reads what the member actually received (frame rate and size from the connection bar). The point is a number that
can be compared before and after a change, on the same screen content, so choose a monitor with something moving on it.

Bounded: a watchdog ends the process after 150 seconds whatever happens.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
    "--ignore-certificate-errors --allow-loopback-in-peer-connection --disable-features=WebRtcHideLocalIpsWithMdns"
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import webview  # noqa: E402

from privatepartynet.app import WindowApi, webview_options  # noqa: E402
from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.frames import ScreenGrabber  # noqa: E402
from privatepartynet.server import Party  # noqa: E402

ARGS = None
CORES = psutil.cpu_count()

# A busy picture at 60 frames a second: many moving shapes, scrolling text and a drifting gradient. It is not a game, but
# it changes everywhere on every frame, which is what makes a screen costly to encode.
MOTION = """<!doctype html><html><body style="margin:0;background:#000;overflow:hidden"><canvas id="c"></canvas><script>
var c = document.getElementById("c"), ctx = c.getContext("2d");
function fit() { c.width = innerWidth; c.height = innerHeight; }
fit(); addEventListener("resize", fit);
var shapes = []; for (var i = 0; i < 400; i++) shapes.push({ x: Math.random(), y: Math.random(), s: 20 + Math.random() * 120, vx: (Math.random() - .5) * .01, vy: (Math.random() - .5) * .01, h: Math.random() * 360 });
var t = 0;
function frame() {
  t += 1;
  var g = ctx.createLinearGradient(0, 0, c.width, c.height);
  g.addColorStop(0, "hsl(" + (t % 360) + ",60%,25%)"); g.addColorStop(1, "hsl(" + ((t * 2 + 120) % 360) + ",60%,35%)");
  ctx.fillStyle = g; ctx.fillRect(0, 0, c.width, c.height);
  shapes.forEach(function (s) {
    s.x = (s.x + s.vx + 1) % 1; s.y = (s.y + s.vy + 1) % 1;
    ctx.fillStyle = "hsla(" + ((s.h + t) % 360) + ",80%,55%,.75)"; ctx.fillRect(s.x * c.width, s.y * c.height, s.s, s.s * .7);
  });
  ctx.fillStyle = "#fff"; ctx.font = "28px monospace";
  for (var r = 0; r < 40; r++) ctx.fillText("Heroes of the porch " + ((t * 7 + r * 131) % 99999) + " gold 12345 wood 678 ore 90", 20, ((r * 36 - t * 3) % c.height + c.height) % c.height);
  requestAnimationFrame(frame);
}
frame();
</script></body></html>"""


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


def category(proc):
    try:
        if proc.pid == os.getpid():
            return "python"
        args = " ".join(proc.cmdline())
    except psutil.Error:
        return None
    for kind in ("renderer", "gpu-process", "utility", "crashpad-handler"):
        if f"--type={kind}" in args:
            return {"gpu-process": "gpu"}.get(kind, "renderer" if kind == "renderer" else "other")
    return "browser" if "msedgewebview2" in (proc.name() or "").lower() else "other"


def tree_seconds():
    me = psutil.Process(os.getpid())
    out = {}
    for proc in [me, *me.children(recursive=True)]:
        try:
            times = proc.cpu_times()
        except psutil.Error:
            continue
        kind = category(proc)
        if kind:
            out[proc.pid] = (kind, times.user + times.system)
    return out


def run(host_window, member_window):
    result = {"ok": False}
    try:
        for name, w in (("host", host_window), ("member", member_window)):
            if not wait_for(w, "!!document.getElementById('goLive') && !!window.pywebview", 40):
                print(f"{name} window did not load")
                return
        if ARGS.codec:
            for w in (host_window, member_window):
                js(w, f"localStorage.setItem('privatepartynet.codec', {json.dumps(ARGS.codec)}); location.reload(); true")
            time.sleep(1.0)
            for w in (host_window, member_window):
                wait_for(w, "!!document.getElementById('goLive') && !!window.pywebview", 40)
        wait_for(host_window, "document.getElementById('count').textContent.startsWith('2 people')", 30)
        wait_for(host_window, f"document.querySelectorAll('#monitor option').length >= {ARGS.monitor}", 20)
        js(host_window, f"var s = document.getElementById('monitor'); s.value = '{ARGS.monitor}'; s.dispatchEvent(new Event('change')); "
                        f"var f = document.getElementById('fps'); f.value = '{ARGS.fps}'; f.dispatchEvent(new Event('change')); "
                        f"var q = document.getElementById('ceiling'); q.value = '{ARGS.ceiling}'; q.dispatchEvent(new Event('change')); true")
        js(host_window, "document.getElementById('goLive').click(); true")
        got = wait_for(member_window, "(function () { var v = document.querySelector('.tile video'); return !!v && v.videoWidth > 0; })()", 40)
        if not got:
            print("the member never received video")
            return
        time.sleep(8)  # warm-up: the encoder settles on its rate and size
        before, t0 = tree_seconds(), time.monotonic()
        time.sleep(ARGS.seconds)
        after, wall = tree_seconds(), time.monotonic() - t0
        shares = {}
        for pid, (kind, secs) in after.items():
            if pid in before:
                shares[kind] = shares.get(kind, 0.0) + (secs - before[pid][1]) / (wall * CORES) * 100
        seen = js(member_window, "JSON.stringify([...document.querySelectorAll('#peerChips .chip')].map(c => ({ name: c.querySelector('.cname').textContent, fps: c.dataset.downFps, res: c.dataset.downRes, kbps: c.dataset.downKbps })))")
        grown = None
        if ARGS.fullscreen_check:
            before_size = js(host_window, "JSON.parse(window.PartyDebug())[0].feedHeight")
            js(member_window, "document.querySelector('.tile .tool.fullscreen').click(); true")
            end = time.monotonic() + 15
            after_size = before_size
            while time.monotonic() < end:
                time.sleep(1)
                after_size = js(host_window, "JSON.parse(window.PartyDebug())[0].feedHeight")
                if after_size != before_size:
                    break
            time.sleep(3)
            res = js(member_window, "JSON.stringify([...document.querySelectorAll('#peerChips .chip')].map(c => c.dataset.downRes))")
            video = js(member_window, "JSON.stringify((function () { var v = document.querySelector('.tile video'); return { videoWidth: v.videoWidth, videoHeight: v.videoHeight, inner: innerWidth + 'x' + innerHeight, dpr: devicePixelRatio }; })())")
            host_view = js(host_window, "window.PartyDebug()")
            grown = {"capture_before": before_size, "capture_after_fullscreen": after_size, "member_receives": json.loads(res), "member_video": json.loads(video), "host_view": json.loads(host_view)}
            js(member_window, "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); true")
        result = {"ok": True, "fullscreen_check": grown, "codec": ARGS.codec or "default", "monitor": ARGS.monitor, "fps_target": ARGS.fps, "ceiling": ARGS.ceiling, "seconds": round(wall, 1),
                  "cpu_pct_of_pc": {k: round(v, 2) for k, v in sorted(shares.items())}, "total_pct": round(sum(shares.values()), 2),
                  "member_received": json.loads(seen)}
    except Exception as err:
        result = {"ok": False, "error": repr(err)}
    finally:
        print("PROFILE " + json.dumps(result), flush=True)
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--codec", default="")
    parser.add_argument("--ceiling", default="source", help="the Go Live quality ceiling: source, 1080, 720 or 480")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--fullscreen-check", action="store_true", help="afterwards, make the member's stream fullscreen and see the capture grow")
    parser.add_argument("--motion", action="store_true", help="cover the shared monitor with an animation, a stand-in for a game")
    ARGS = parser.parse_args()
    folder = tempfile.mkdtemp(prefix="privatepartynet-cpu-profile-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("315902", 8795, certfile, keyfile, party_name="CPU profile", host_name="Host", grabber=ScreenGrabber())
    party.start()
    local = f"http://127.0.0.1:{party.local_port}"
    host_api = WindowApi(local)
    host_window = webview.create_window("CPU profile host", url=local + "/host", js_api=host_api, hidden=not ARGS.visible,
                                        focus=False, width=900, height=560, x=3500, y=40)
    host_api._attach(host_window)
    member_origin = f"https://localhost:{party.port}"
    member_api = WindowApi(member_origin)
    member_window = webview.create_window("CPU profile member", url=f"{member_origin}/join?token={party.join_token('Alex')}", js_api=member_api,
                                          hidden=not ARGS.visible, focus=False, width=900, height=560, x=4500, y=40)
    member_api._attach(member_window)
    if ARGS.motion:
        from privatepartynet.frames import list_monitors

        shared = next(m for m in list_monitors() if m["index"] == ARGS.monitor)
        webview.create_window("CPU profile motion", html=MOTION, frameless=True, on_top=True, focus=False,
                              x=shared["left"], y=shared["top"], width=shared["width"], height=shared["height"])
    watchdog = threading.Timer(150, lambda: os._exit(2))
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(lambda: run(host_window, member_window), **webview_options(Path(folder) / "webview"))
    finally:
        watchdog.cancel()
        party.stop()


if __name__ == "__main__":
    main()
