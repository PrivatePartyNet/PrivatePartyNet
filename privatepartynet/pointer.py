"""The mouse pointer, drawn onto shared screen frames.

Neither capture path puts the pointer in the picture: Desktop Duplication reports it separately and GDI leaves it out.
A share therefore asks Windows where the pointer is and what it looks like, and blends it onto each frame before the
frame is encoded.

Windows draws a pointer's picture onto a bitmap of our choosing. It is drawn twice, once over black and once over
white; the difference between the two gives each pixel's transparency, and the black rendering is the pointer's color
already multiplied by it. That handles pointers with soft edges, pointers made of a mask, and the text cursor, which
inverts whatever is under it (those pixels turn black on one rendering and white on the other).

blend() and sprite_from_renders() are plain array code, so the tests run them anywhere. Only read_pointer() and
render_pointer() talk to Windows.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger("privatepartynet.pointer")

CURSOR_SHOWING = 0x1
DI_NORMAL = 0x3
SPRITE_CACHE_LIMIT = 24
MAX_SIDE = 256  # a pointer is never larger than this; anything bigger is a mistake in what Windows told us

_api = False  # False: not loaded yet. None: not Windows. Otherwise the loaded functions.


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _CursorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD), ("handle", wintypes.HANDLE), ("pos", _Point)]


class _IconInfo(ctypes.Structure):
    _fields_ = [("is_icon", wintypes.BOOL), ("hot_x", wintypes.DWORD), ("hot_y", wintypes.DWORD),
                ("mask", wintypes.HBITMAP), ("color", wintypes.HBITMAP)]


class _Bitmap(ctypes.Structure):
    _fields_ = [("type", wintypes.LONG), ("width", wintypes.LONG), ("height", wintypes.LONG),
                ("width_bytes", wintypes.LONG), ("planes", wintypes.WORD), ("bits_pixel", wintypes.WORD),
                ("bits", wintypes.LPVOID)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                ("planes", wintypes.WORD), ("bit_count", wintypes.WORD), ("compression", wintypes.DWORD),
                ("size_image", wintypes.DWORD), ("x_ppm", wintypes.LONG), ("y_ppm", wintypes.LONG),
                ("clr_used", wintypes.DWORD), ("clr_important", wintypes.DWORD)]


def _load():
    global _api
    if _api is not False:
        return _api
    if sys.platform != "win32":
        _api = None
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CursorInfo)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IconInfo)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DrawIconEx.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GdiFlush.argtypes = []
    try:
        user32.SetProcessDPIAware()  # pointer and monitor coordinates must both be in real pixels; mss does the same
    except Exception:
        pass
    _api = (user32, gdi32)
    return _api


def read_pointer():
    """(handle, x, y) of the pointer in desktop pixels, or None when it is hidden or Windows will not say."""
    api = _load()
    if api is None:
        return None
    info = _CursorInfo()
    info.size = ctypes.sizeof(_CursorInfo)
    if not api[0].GetCursorInfo(ctypes.byref(info)) or not (info.flags & CURSOR_SHOWING) or not info.handle:
        return None
    return int(info.handle), int(info.pos.x), int(info.pos.y)


def render_pointer(handle):
    """(over_black, over_white, hot_x, hot_y) for a pointer handle: two height x width x 4 BGRA arrays. None on failure."""
    import numpy

    api = _load()
    if api is None:
        return None
    user32, gdi32 = api
    icon = _IconInfo()
    if not user32.GetIconInfo(handle, ctypes.byref(icon)):
        return None
    screen = memory = bitmap = previous = None
    try:
        probe = _Bitmap()
        source = icon.color or icon.mask
        if not gdi32.GetObjectW(source, ctypes.sizeof(_Bitmap), ctypes.byref(probe)):
            return None
        width = probe.width
        height = probe.height if icon.color else probe.height // 2  # a mask-only pointer stacks its AND and XOR masks
        if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
            return None
        header = _BitmapInfoHeader()
        header.size = ctypes.sizeof(_BitmapInfoHeader)
        header.width, header.height, header.planes, header.bit_count = width, -height, 1, 32  # top-down, 32 bits
        bits = ctypes.c_void_p()
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateDIBSection(memory, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            return None
        previous = gdi32.SelectObject(memory, bitmap)
        renders = []
        for fill in (0x00, 0xFF):
            ctypes.memset(bits, fill, width * height * 4)
            if not user32.DrawIconEx(memory, 0, 0, handle, width, height, 0, None, DI_NORMAL):
                return None
            gdi32.GdiFlush()
            raw = ctypes.string_at(bits.value, width * height * 4)
            renders.append(numpy.frombuffer(raw, dtype=numpy.uint8).reshape(height, width, 4).copy())
        return renders[0], renders[1], int(icon.hot_x), int(icon.hot_y)
    finally:
        if memory and previous:
            gdi32.SelectObject(memory, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        if screen:
            user32.ReleaseDC(None, screen)
        for part in (icon.color, icon.mask):
            if part:
                gdi32.DeleteObject(part)


def sprite_from_renders(over_black, over_white, hot_x, hot_y):
    """A drawable pointer from its two renderings: (premultiplied color, 1 - alpha, invert mask, hot_x, hot_y).

    Over black a pixel shows color * alpha. Over white it shows color * alpha + 255 * (1 - alpha). The difference is
    therefore 255 * (1 - alpha). A pixel that inverts the screen comes out white over black and black over white,
    a difference of -255, and is marked for inversion instead.
    """
    import numpy

    black = over_black[:, :, :3].astype(numpy.int16)
    white = over_white[:, :, :3].astype(numpy.int16)
    diff = white - black
    invert = diff.max(axis=2) < -127
    see_through = numpy.clip(diff.mean(axis=2) / 255.0, 0.0, 1.0)  # 1 - alpha
    see_through[invert] = 1.0
    return over_black[:, :, :3].copy(), see_through.astype(numpy.float32)[:, :, None], invert, hot_x, hot_y


def blend(frame, sprite, x, y):
    """Draw a sprite onto a BGRA frame in place, its hot spot at (x, y) in frame pixels.

    Returns what is needed to take it off again with restore(), or None when none of it falls on the frame.
    """
    import numpy

    color, see_through, invert, hot_x, hot_y = sprite
    height, width = frame.shape[0], frame.shape[1]
    left, top = x - hot_x, y - hot_y
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + color.shape[1], width), min(top + color.shape[0], height)
    if x0 >= x1 or y0 >= y1:
        return None
    sx, sy = x0 - left, y0 - top
    region = frame[y0:y1, x0:x1]
    undo = (y0, y1, x0, x1, region.copy())
    window = (slice(sy, sy + (y1 - y0)), slice(sx, sx + (x1 - x0)))
    under = region[:, :, :3].astype(numpy.float32)
    out = color[window].astype(numpy.float32) + under * see_through[window]
    flip = invert[window]
    out[flip] = 255.0 - under[flip]
    region[:, :, :3] = numpy.clip(out, 0, 255).astype(numpy.uint8)
    return undo


def restore(frame, undo):
    """Put back what blend() covered."""
    if undo is not None:
        y0, y1, x0, x1, patch = undo
        frame[y0:y1, x0:x1] = patch


class Pointer:
    """The pointer over one monitor. Windows' pictures of pointers are drawn once each and kept."""

    def __init__(self, monitor, read=None, render=None):
        self.left, self.top = monitor["left"], monitor["top"]
        self.width, self.height = monitor["width"], monitor["height"]
        self._read = read or read_pointer
        self._render = render or render_pointer
        self._sprites = {}

    def where(self):
        """(handle, x, y) with x and y inside this monitor, or None when the pointer is hidden or on another monitor."""
        try:
            seen = self._read()
        except Exception:
            log.debug("Reading the pointer failed", exc_info=True)
            return None
        if seen is None:
            return None
        handle, x, y = seen
        x, y = x - self.left, y - self.top
        if not (-MAX_SIDE < x < self.width + MAX_SIDE and -MAX_SIDE < y < self.height + MAX_SIDE):
            return None
        return handle, x, y

    def _sprite(self, handle):
        if handle not in self._sprites:
            if len(self._sprites) >= SPRITE_CACHE_LIMIT:
                self._sprites.pop(next(iter(self._sprites)))
            try:
                rendered = self._render(handle)
            except Exception:
                log.debug("Drawing a pointer failed", exc_info=True)
                rendered = None
            self._sprites[handle] = sprite_from_renders(*rendered) if rendered is not None else None
        return self._sprites[handle]

    def draw(self, frame, where):
        """Blend the pointer onto a BGRA frame at the position where() gave. Returns the undo for restore()."""
        if where is None:
            return None
        handle, x, y = where
        sprite = self._sprite(handle)
        if sprite is None:
            return None
        return blend(frame, sprite, x, y)
