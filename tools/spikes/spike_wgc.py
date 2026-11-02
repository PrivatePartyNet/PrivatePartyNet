"""Spike: does Windows.Graphics.Capture (the `windows-capture` package) see a screen, and how fast?

The work queue's open item is borderless-fullscreen games, which Desktop Duplication cannot see (it freezes) and the GDI
fallback shows at 15 to 19 fps at 3440x1440. This script captures one monitor through Windows.Graphics.Capture for a few
seconds and reports the frame count, the size and the frame rate, and how long a conversion to BGR takes (the step the
JPEG encoder needs). Run it while something moves on that screen (a video, a game); a still desktop produces few frames
because the capture only delivers when the picture changes.

    py tools\\spikes\\spike_wgc.py [monitor_index] [seconds]

Bounded: the capture is stopped after `seconds` (default 8, at most 30) and the script exits. Nothing is shown on screen.
The package is expected in build\\_tmp\\wgc_site (pip install --target build\\_tmp\\wgc_site windows-capture); it is not
part of the app.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "build" / "_tmp" / "wgc_site"))

from windows_capture import WindowsCapture  # noqa: E402


def main():
    monitor = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    seconds = max(2.0, min(30.0, float(sys.argv[2]) if len(sys.argv) > 2 else 8.0))
    stats = {"frames": 0, "size": None, "convert_ms": [], "first": None, "last": None}
    capture = WindowsCapture(cursor_capture=True, draw_border=False, monitor_index=monitor)

    @capture.event
    def on_frame_arrived(frame, control):
        now = time.perf_counter()
        stats["first"] = stats["first"] or now
        stats["last"] = now
        stats["frames"] += 1
        stats["size"] = (frame.width, frame.height)
        if stats["frames"] % 10 == 1:
            start = time.perf_counter()
            frame.convert_to_bgr()
            stats["convert_ms"].append((time.perf_counter() - start) * 1000)

    @capture.event
    def on_closed():
        pass

    control = capture.start_free_threaded()
    timer = threading.Timer(seconds, control.stop)
    timer.daemon = True
    timer.start()
    control.wait()
    timer.cancel()
    if not stats["frames"]:
        print("no frames arrived")
        return 1
    span = max(1e-6, stats["last"] - stats["first"])
    print(f"monitor {monitor}: {stats['frames']} frames at {stats['size'][0]}x{stats['size'][1]} "
          f"in {span:.1f} s = {stats['frames'] / span:.1f} fps")
    if stats["convert_ms"]:
        print(f"convert to BGR: median {sorted(stats['convert_ms'])[len(stats['convert_ms']) // 2]:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
