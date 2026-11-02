"""Screen frames for the windows on this PC, grabbed in Python.

A party window does not use the browser's screen picker. Instead the page asks this PC's loopback server for frames:
a length-prefixed JPEG stream, one frame at a time, paced to the frame rate chosen. The page draws each frame to a
canvas and shares that canvas over WebRTC like any other screen. Nothing here runs until someone goes live, and it
stops when they stop or the page goes away.

Capture uses the GPU's Desktop Duplication (the dxcam package) when it can. On an ultrawide screen it delivers well
over 100 frames a second, where the older GDI capture (mss) tops out near 30. mss stays as the fallback for a PC or a
moment where Desktop Duplication is not available (no capture device, a locked screen, the output already in use).

Encoding uses OpenCV when it is installed. OpenCV releases Python's global lock while it converts and encodes, so the
grabbing thread keeps running during the encode; Pillow does not, and is the slower fallback.
"""

import io
import logging
import threading
import time

from PIL import Image

from privatepartynet.pointer import restore

log = logging.getLogger("privatepartynet.capture")

JPEG_QUALITY = 70
MIN_HEIGHT, MAX_HEIGHT = 240, 2160
STATIC_REPEAT = 0.5  # a still screen sends its last frame this often, so a new viewer is never left on a blank picture
FIRST_FRAME_WAIT = 2.0  # seconds without a first frame before the GPU path is given up on
STALL_CHECK = 1.0  # seconds without a GPU frame before the GDI view of the screen is compared with the last frame
STALL_CHANGED = 0.01  # the share of sampled pixels that must differ for the screen to count as changed
SEED_WAIT = 0.6  # a screen that is not changing gives Desktop Duplication nothing to send; one GDI grab starts the stream
OPEN_RETRY_SECONDS = 1.5  # a share that restarts may open its output a moment before the old one has let go of it
CV2_THREADS = 2  # OpenCV defaults to one thread per core. A screen share does not need them, and the CPU readout shows it.

_cv2 = False  # False: not looked for yet. None: not available. Otherwise the module.


def _opencv():
    global _cv2
    if _cv2 is False:
        try:
            import cv2

            cv2.setNumThreads(CV2_THREADS)
            _cv2 = cv2
        except Exception:
            log.info("OpenCV is not available; encoding with Pillow")
            _cv2 = None
    return _cv2


def _pillow_jpeg(image):
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def encode_bgra(frame, max_height):
    """A height x width x 4 BGRA array (from dxcam or mss) as JPEG bytes, no taller than max_height.

    With OpenCV the ceiling is met exactly. With Pillow only a whole-number reduction is done (2, 3, ...), because a
    fractional resize costs more than the encode itself; the browser's WebRTC encoder scales to the ceiling anyway.
    """
    cv2 = _opencv()
    height, width = frame.shape[0], frame.shape[1]
    if cv2 is not None:
        if max_height and height > max_height:
            target = (round(width * max_height / height), max_height)
            # Exact halves take OpenCV's fast averaging path (1.2 ms at 3440x1440, against 10 to 13 ms for averaging
            # straight down to a small size), so halve while the picture is at least twice the target. What is left is
            # a reduction of less than half, where linear is cheap and does not alias noticeably.
            while frame.shape[0] >= 2 * max_height and frame.shape[1] >= 2 * target[0]:
                frame = cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2), interpolation=cv2.INTER_AREA)
            if frame.shape[0] != target[1] or frame.shape[1] != target[0]:
                frame = cv2.resize(frame, target, interpolation=cv2.INTER_LINEAR)
        ok, data = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise RuntimeError("OpenCV could not encode the frame")
        return data.tobytes()
    image = Image.frombuffer("RGB", (width, height), frame, "raw", "BGRX", 0, 1)
    if max_height and height > max_height and height // max_height >= 2:
        image = image.reduce(height // max_height)
    return _pillow_jpeg(image)


def _shot_array(shot):
    """A writable height x width x 4 BGRA array over an mss screenshot, without copying it. mss's own .bgra property makes
    a whole-frame copy (twenty megabytes at 3440x1440) on every call; .raw is the same bytes in a bytearray."""
    import numpy

    data = getattr(shot, "raw", None)
    if data is None or isinstance(data, bytes):
        data = shot.bgra
    array = numpy.frombuffer(data, dtype=numpy.uint8).reshape(shot.height, shot.width, 4)
    return array if array.flags.writeable else array.copy()


def encode_jpeg(shot, max_height, pointer=None, where=None):
    """An mss screenshot as JPEG bytes, with the mouse pointer drawn on it when where (from Pointer.where) says so."""
    cv2 = _opencv()
    if cv2 is not None or where is not None:
        import numpy

        array = _shot_array(shot)
        if where is not None:
            pointer.draw(array, where)
        return encode_bgra(array, max_height)
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    if max_height and image.height > max_height:
        width = round(image.width * max_height / image.height)
        image = image.resize((width, max_height), Image.Resampling.BILINEAR)
    return _pillow_jpeg(image)


def list_monitors():
    """The screens this PC has, numbered from 1 in a fixed order: left to right, then top to bottom.

    mss lists them in the order Windows enumerates them, and that order is not fixed: on the same PC with the same screens
    the "second" monitor was a different screen on a later call, so a share could capture another screen than the one
    chosen. Sorting by position gives every screen the same number each time. Screens are then identified by their place
    on the desktop (left, top, width, height) wherever they are captured.
    """
    import mss

    with mss.MSS() as sct:
        found = [m for m in sct.monitors[1:]]
    found.sort(key=lambda m: (m["left"], m["top"], m["width"], m["height"]))
    return [
        {
            "index": index, "width": m["width"], "height": m["height"], "left": m["left"], "top": m["top"],
            "primary": m["left"] == 0 and m["top"] == 0,
        }
        for index, m in enumerate(found, start=1)
    ]


def _primary_index():
    """The number of the main screen (the one at the desktop's origin), or 1."""
    try:
        return next(m["index"] for m in list_monitors() if m["primary"])
    except (StopIteration, Exception):
        return 1


def _region(monitor_index):
    """The desktop rectangle of a monitor, as mss takes it, or None."""
    try:
        m = next(m for m in list_monitors() if m["index"] == monitor_index)
    except StopIteration:
        return None
    return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}


# --- Desktop Duplication (dxcam) ---------------------------------------------------------------------------------

_dx_lock = threading.Lock()
_dx_busy = set()  # (device, output) pairs a stream is using. One stream per output.


def match_output(monitor, outputs):
    """The (device, output) of the GPU output whose desktop rectangle is this monitor's, or None.

    outputs: tuples of (device, output, left, top, width, height), as the GPU reports them. Both capture libraries use
    desktop coordinates, so a monitor and its output share the same left, top, width and height.
    """
    wanted = (monitor["left"], monitor["top"], monitor["width"], monitor["height"])
    for device, output, left, top, width, height in outputs:
        if (left, top, width, height) == wanted:
            return device, output
    return None


def _dxgi_outputs(dxcam):
    factory = getattr(dxcam, "__factory", None)
    found = []
    for device, outputs in enumerate(factory.outputs if factory is not None else []):
        for output, item in enumerate(outputs):
            rect = item.desc.DesktopCoordinates
            found.append((device, output, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
    return found


def _open_dxgi(monitor_index):
    """(camera, key) for a monitor, or None when Desktop Duplication cannot be used for it right now."""
    try:
        import dxcam

        monitor = next(m for m in list_monitors() if m["index"] == monitor_index)
        key = match_output(monitor, _dxgi_outputs(dxcam))
    except Exception as err:  # no package, no capture device, an unexpected shape: fall back quietly
        log.info("Desktop Duplication is not available: %s", err)
        return None
    if key is None:
        log.info("No GPU output matches monitor %s; using the fallback capture", monitor_index)
        return None
    # A share that restarts (a new quality or frame rate) opens its output while the old stream is still winding down:
    # the server only notices the page has gone at its next write. Wait a moment for it rather than fall back for good.
    deadline = time.monotonic() + OPEN_RETRY_SECONDS
    while True:
        with _dx_lock:
            if key not in _dx_busy:
                _dx_busy.add(key)
                break
        if time.monotonic() > deadline:
            log.info("Output %s is already being captured; using the fallback capture", key)
            return None
        time.sleep(0.05)
    deadline = time.monotonic() + OPEN_RETRY_SECONDS
    while True:
        try:
            camera = dxcam.create(device_idx=key[0], output_idx=key[1], output_color="BGRA", processor_backend="numpy")
            return camera, key
        except Exception as err:
            if time.monotonic() > deadline:
                log.info("Could not open output %s: %s", key, err)
                with _dx_lock:
                    _dx_busy.discard(key)
                return None
            time.sleep(0.1)


def _close_dxgi(camera, key):
    try:
        camera.release()
    except Exception:
        log.debug("Releasing the capture raised", exc_info=True)
    with _dx_lock:
        _dx_busy.discard(key)


def screens_differ(a, b):
    """True when two BGRA screens of the same size are visibly different: a coarse grid of pixels is compared, so a
    clock or a pointer does not count and a game or a video does."""
    import numpy

    if a is None or b is None or a.shape != b.shape:
        return False
    first = a[::24, ::24, :3].astype(numpy.int16)
    second = b[::24, ::24, :3].astype(numpy.int16)
    return float((numpy.abs(first - second).max(axis=2) > 24).mean()) > STALL_CHANGED


def _dxgi_frames(camera, fps, max_height, pointer=None, seed=None):
    """JPEG frames from an open dxcam camera, paced to fps. A still screen repeats its last frame twice a second.

    With a pointer (see pointer.py) the mouse pointer is drawn on every frame. Desktop Duplication only delivers a
    frame when the picture changes, so a pointer moving over a still screen draws the kept frame again at the new
    place, and the screen stays as lively as the hand moving the mouse.

    Desktop Duplication only sends a frame when the picture changes, so a screen that is perfectly still sends none. When
    no first frame has come after SEED_WAIT seconds, seed() (one GDI grab, a BGRA array) supplies it and the stream goes on
    from there, instead of giving up on the GPU path.

    Some screens send Desktop Duplication nothing even while they change (a borderless fullscreen game that DWM presents
    directly, for one). While seed is given, a GPU stream that has been silent for STALL_CHECK seconds is checked against
    one GDI grab: if the screen has visibly changed, this raises RuntimeError and the caller carries on with GDI, which
    sees what is on the screen. A screen that is really still passes the check and keeps repeating its frame.

    Two threads share the work: one grabs the newest screen at the target pace, and this generator encodes whatever
    is newest. Grabbing then overlaps the encode, and a slow encode drops frames instead of slowing the capture.
    Raises RuntimeError when no first frame arrives, so the caller can fall back to the other capture, and passes on
    anything the grabbing thread raised. The caller releases the camera, after this generator has been closed.
    """
    interval = 1.0 / fps
    stop = threading.Event()
    ready = threading.Condition()
    newest = []  # at most one raw frame, waiting to be encoded
    failure = []

    def grab_loop():
        next_at = time.perf_counter()
        try:
            while not stop.is_set():
                wait = next_at - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)  # a high-resolution wait on Python 3.11 and later; Event.wait rounds to 15 ms
                next_at = max(next_at + interval, time.perf_counter() - interval)  # never catch up by more than a frame
                frame = camera.grab()
                with ready:
                    if frame is not None:
                        newest[:] = [frame]
                    ready.notify()
        except Exception as err:
            with ready:
                failure.append(err)
                ready.notify()

    worker = threading.Thread(target=grab_loop, name="privatepartynet-dxgi-grab", daemon=True)
    worker.start()
    started = time.perf_counter()
    last = None
    last_at = started
    raw = undo = shown = None  # the newest screen, what the pointer covered on it, and where the pointer was drawn
    last_gpu = last_check = started  # the last frame the GPU sent, and the last time the GDI view was compared
    grab_gdi = seed  # kept for the stall check; seed itself is used up by the first frame
    try:
        while True:
            with ready:
                if not newest and not failure:
                    ready.wait(max(0.02, STATIC_REPEAT / 2))
                if failure:
                    raise failure[0]
                frame = newest.pop() if newest else None
            now = time.perf_counter()
            if frame is not None:
                last_gpu = now
            where = pointer.where() if pointer is not None else None
            if frame is not None:
                raw = frame
                undo = pointer.draw(raw, where) if pointer is not None else None
                shown = where
                last = encode_bgra(raw, max_height)
                last_at = time.perf_counter()
                yield last
            elif last is None:
                if seed is not None and now - started > SEED_WAIT:
                    seeded, seed = seed(), None  # once only
                    if seeded is not None:
                        raw = seeded
                        last_gpu = last_check = now
                        undo = pointer.draw(raw, where) if pointer is not None else None
                        shown = where
                        last = encode_bgra(raw, max_height)
                        last_at = time.perf_counter()
                        yield last
                        continue
                if now - started > FIRST_FRAME_WAIT:
                    raise RuntimeError("no first frame from Desktop Duplication")
            elif pointer is not None and where != shown and now - last_at >= interval:
                restore(raw, undo)
                undo = pointer.draw(raw, where)
                shown = where
                last = encode_bgra(raw, max_height)
                last_at = time.perf_counter()
                yield last
            elif now - last_at >= STATIC_REPEAT:
                last_at = now
                yield last
            if grab_gdi is not None and raw is not None and now - last_gpu >= STALL_CHECK and now - last_check >= STALL_CHECK:
                last_check = now
                current = grab_gdi()
                if screens_differ(current, raw):
                    raise RuntimeError("Desktop Duplication has sent nothing while the screen changes")
    finally:
        stop.set()
        worker.join(timeout=2)


# --- GDI capture (mss), the fallback -----------------------------------------------------------------------------

def _mss_array(monitor):
    """One GDI grab of a monitor as a writable height x width x 4 BGRA array, or None."""
    try:
        import mss
        import numpy

        region = _region(monitor)
        if region is None:
            return None
        with mss.MSS() as sct:
            return _shot_array(sct.grab(region))
    except Exception:
        log.debug("The first-frame grab failed", exc_info=True)
        return None


def _mss_frames(monitor, fps, max_height, pointer=None):
    """JPEG frames at about fps. Capture runs on its own thread, on a fixed schedule, and this generator encodes the
    newest grab. A slow encode drops frames instead of slowing the capture. Closing it stops the capture thread."""
    import mss  # imported here, so the tests and tools that never capture do not need it

    interval = 1.0 / fps
    stop = threading.Event()
    ready = threading.Condition()
    newest = []

    region = _region(monitor)
    if region is None:
        raise RuntimeError(f"No screen {monitor}")

    def capture():
        with mss.MSS() as sct:
            next_at = time.monotonic()
            while not stop.is_set():
                shot = sct.grab(region)
                with ready:
                    newest[:] = [shot]
                    ready.notify()
                next_at += interval
                pause = next_at - time.monotonic()
                if pause > 0:
                    stop.wait(pause)
                else:
                    next_at = time.monotonic()

    worker = threading.Thread(target=capture, name="privatepartynet-capture", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            with ready:
                if not newest:
                    ready.wait(1.0)
                if not newest:
                    continue
                shot = newest.pop()
            yield encode_jpeg(shot, max_height, pointer, pointer.where() if pointer is not None else None)
    finally:
        stop.set()
        worker.join(timeout=2)


def _pointer_for(monitor_index):
    """The Pointer for a monitor, or None when it cannot be made (no such monitor, not Windows, no numpy)."""
    try:
        import numpy  # noqa: F401

        from privatepartynet.pointer import Pointer

        monitor = next(m for m in list_monitors() if m["index"] == monitor_index)
        return Pointer(monitor)
    except Exception as err:
        log.info("The mouse pointer will not be shown: %s", err)
        return None


class ScreenGrabber:
    """Captures one monitor. Use frames() as a generator; closing it releases the screen capture."""

    def __init__(self, monitor=None):
        self.monitor = monitor  # None: the main screen, found when the capture starts
        self.backend = None  # "dxgi" or "mss", set when frames() starts

    def for_monitor(self, monitor):
        return ScreenGrabber(monitor)

    def monitors(self):
        return list_monitors()

    def frames(self, fps, max_height, pointer=True):
        """JPEG frames at about fps, from the GPU when possible and from GDI otherwise. The mouse pointer is drawn
        on them unless pointer is False."""
        if self.monitor is None:
            self.monitor = _primary_index()
        pointer = _pointer_for(self.monitor) if pointer else None
        opened = _open_dxgi(self.monitor)
        if opened is not None:
            camera, key = opened
            try:
                self.backend = "dxgi"
                log.info("Capture for monitor %s: Desktop Duplication, %s fps target", self.monitor, fps)
                yield from _dxgi_frames(camera, fps, max_height, pointer, seed=lambda: _mss_array(self.monitor))
                return
            except Exception:
                log.exception("Desktop Duplication stopped; switching to the fallback capture")
            finally:
                _close_dxgi(camera, key)
        self.backend = "mss"
        log.info("Capture for monitor %s: fallback (GDI), %s fps target", self.monitor, fps)
        yield from _mss_frames(self.monitor, fps, max_height, pointer)


def clamp_height(value):
    """A requested frame height, or None for the source size. Out-of-range values are treated as the source size."""
    try:
        height = int(value)
    except (TypeError, ValueError):
        return None
    return height if MIN_HEIGHT <= height <= MAX_HEIGHT else None


def clamp_fps(value):
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return 30
    return fps if fps in (15, 30, 60) else 30


def check_capture(seconds=1.5):
    """Grab real frames of this PC's screen for a moment and check they are JPEGs. Run as: PrivatePartyNet.exe --check-capture.

    Returns 0 when frames came back, 1 otherwise. The backend, the encoder and the rate are written to the log.
    """
    grabber = ScreenGrabber()
    frames = 0
    size = 0
    started = time.perf_counter()
    stream = grabber.frames(fps=30, max_height=720)
    try:
        for frame in stream:
            if not frame.startswith(b"\xff\xd8"):
                log.error("Screen capture returned something other than a JPEG")
                return 1
            frames += 1
            size = len(frame)
            if time.perf_counter() - started >= seconds:
                break
    except Exception:
        log.exception("Screen capture failed")
        return 1
    finally:
        stream.close()
    took = time.perf_counter() - started
    if not frames:
        log.error("Screen capture returned no frames")
        return 1
    log.info("Screen capture check passed: backend %s, encoder %s, %d frames in %.1f s (%.0f per second), last frame %d bytes",
             grabber.backend, "OpenCV" if _opencv() is not None else "Pillow", frames, took, frames / took, size)
    return 0
