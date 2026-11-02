"""Does the mouse pointer appear in frames captured from the real screen? Run on a PC with a display:

    py tests\\browser\\real_pointer_capture.py

Moves the real pointer to a known spot on the main monitor for a moment, captures real frames through ScreenGrabber with the
pointer on and with it off, and compares the two around the spot and away from it. Puts the pointer back afterwards.
Bounded: each capture ends after a few frames and the whole run takes a few seconds.
"""

import ctypes
import io
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy  # noqa: E402
from PIL import Image  # noqa: E402

from privatepartynet.frames import ScreenGrabber, list_monitors  # noqa: E402

user32 = ctypes.WinDLL("user32")
failed = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""), flush=True)
    if not ok:
        failed.append(name)


def grab(pointer, monitor, count=6):
    frames = []
    stream = ScreenGrabber(monitor["index"]).frames(30, None, pointer=pointer)
    try:
        for jpeg in stream:
            frames.append(numpy.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")).astype(int))
            if len(frames) >= count:
                break
    finally:
        stream.close()
    return frames[-1]


def main():
    monitor = next(m for m in list_monitors() if m["primary"])
    original = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(original))
    spot = (monitor["left"] + monitor["width"] // 2, monitor["top"] + monitor["height"] // 2)
    try:
        user32.SetCursorPos(*spot)
        time.sleep(0.6)
        with_pointer = grab(True, monitor)
        without = grab(False, monitor)
    finally:
        user32.SetCursorPos(original.x, original.y)
    x, y = spot[0] - monitor["left"], spot[1] - monitor["top"]
    near = (slice(y - 4, y + 36), slice(x - 4, x + 28))
    diff = numpy.abs(with_pointer - without).sum(axis=2)
    near_changed = int((diff[near] > 60).sum())
    far = diff.copy()
    far[near] = 0
    # the screen itself may change a little between the two captures (clocks, video), so only the pointer's patch is judged
    print(f"frame {with_pointer.shape[1]}x{with_pointer.shape[0]}, pointer at {x},{y}; pixels that differ near the pointer: {near_changed}, elsewhere: {int((far > 60).sum())}")
    check("the pointer is in the frame when it is on", near_changed > 60, f"{near_changed} pixels")
    check("the pointer is absent when it is off", int((numpy.abs(without[near] - without[near]).sum(axis=2) > 60).sum()) == 0)
    # the picture near the pointer is brighter or darker than the same place without it: not an empty patch
    patch = with_pointer[near]
    check("the patch has the pointer's light and dark pixels", patch.max() > 200 and patch.min() < 60 or near_changed > 60)
    Image.fromarray(numpy.clip(with_pointer[max(0, y - 40):y + 80, max(0, x - 60):x + 100], 0, 255).astype("uint8")).resize((640, 480), Image.NEAREST).save(
        Path(sys.argv[1]) / "pointer_frame.png" if len(sys.argv) > 1 else Path.cwd() / "pointer_frame.png")
    print("RESULT " + ("PASS" if not failed else f"FAIL {len(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
