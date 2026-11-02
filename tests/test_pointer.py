"""The mouse pointer drawn onto shared frames: turning Windows' two renderings into a sprite, blending it at the right
place, taking it off again, and the capture loop redrawing it when it moves over a still screen."""

import sys
import time
import unittest
from unittest import mock

try:
    import numpy
except ImportError:  # the capture packages are optional at run time; these tests need them
    numpy = None

from privatepartynet import frames, pointer


def solid(width, height, color):
    """A BGRA frame of one color."""
    array = numpy.zeros((height, width, 4), dtype=numpy.uint8)
    array[:, :] = (*color, 255)
    return array


def renders(width, height, pixels):
    """The black and white renderings of a pointer given as {(row, column): (b, g, r, alpha 0..1)}; the rest is clear."""
    black = numpy.zeros((height, width, 4), dtype=numpy.uint8)
    white = numpy.full((height, width, 4), 255, dtype=numpy.uint8)
    for (row, column), (b, g, r, alpha) in pixels.items():
        premultiplied = numpy.array([b, g, r]) * alpha
        black[row, column, :3] = premultiplied
        white[row, column, :3] = premultiplied + 255 * (1 - alpha)
    return black, white


@unittest.skipIf(numpy is None, "numpy is not installed")
class SpriteTests(unittest.TestCase):
    def sprite(self, pixels, width=4, height=4, hot=(0, 0)):
        black, white = renders(width, height, pixels)
        return pointer.sprite_from_renders(black, white, *hot)

    def test_an_opaque_pixel_replaces_what_is_under_it_and_a_clear_one_leaves_it(self):
        sprite = self.sprite({(1, 1): (10, 20, 30, 1.0)})
        frame = solid(4, 4, (200, 100, 50))
        pointer.blend(frame, sprite, 0, 0)
        self.assertEqual(tuple(frame[1, 1, :3]), (10, 20, 30))
        self.assertEqual(tuple(frame[0, 0, :3]), (200, 100, 50))
        self.assertEqual(tuple(frame[3, 3, :3]), (200, 100, 50))

    def test_a_half_transparent_pixel_mixes_with_the_screen(self):
        sprite = self.sprite({(2, 2): (0, 0, 0, 0.5)})
        frame = solid(4, 4, (200, 100, 50))
        pointer.blend(frame, sprite, 0, 0)
        for got, want in zip(frame[2, 2, :3], (100, 50, 25)):
            self.assertLessEqual(abs(int(got) - want), 2)

    def test_a_pixel_that_inverts_the_screen_does_so(self):
        black = numpy.zeros((2, 2, 4), dtype=numpy.uint8)
        white = numpy.full((2, 2, 4), 255, dtype=numpy.uint8)
        black[0, 0, :3], white[0, 0, :3] = 255, 0  # white over black, black over white: the text cursor
        sprite = pointer.sprite_from_renders(black, white, 0, 0)
        frame = solid(2, 2, (240, 240, 240))
        pointer.blend(frame, sprite, 0, 0)
        self.assertEqual(tuple(frame[0, 0, :3]), (15, 15, 15), "light page, dark cursor")
        self.assertEqual(tuple(frame[1, 1, :3]), (240, 240, 240))

    def test_the_hot_spot_lands_on_the_position(self):
        sprite = self.sprite({(1, 2): (255, 255, 255, 1.0)}, hot=(2, 1))
        frame = solid(10, 10, (0, 0, 0))
        pointer.blend(frame, sprite, 5, 5)
        self.assertEqual(tuple(frame[5, 5, :3]), (255, 255, 255))

    def test_a_pointer_partly_off_the_screen_is_cut_and_one_wholly_off_draws_nothing(self):
        sprite = self.sprite({(0, 0): (9, 9, 9, 1.0), (3, 3): (7, 7, 7, 1.0)})
        frame = solid(6, 6, (100, 100, 100))
        self.assertIsNotNone(pointer.blend(frame, sprite, -1, -1))
        self.assertEqual(tuple(frame[2, 2, :3]), (7, 7, 7))
        self.assertIsNone(pointer.blend(frame, sprite, -50, 0))
        self.assertIsNone(pointer.blend(frame, sprite, 0, 600))

    def test_restoring_gives_back_the_original_picture(self):
        sprite = self.sprite({(0, 0): (1, 2, 3, 1.0), (1, 1): (4, 5, 6, 0.5)})
        frame = solid(8, 8, (90, 80, 70))
        before = frame.copy()
        undo = pointer.blend(frame, sprite, 3, 3)
        self.assertFalse(numpy.array_equal(frame, before))
        pointer.restore(frame, undo)
        self.assertTrue(numpy.array_equal(frame, before))
        pointer.restore(frame, None)


@unittest.skipIf(numpy is None, "numpy is not installed")
class PointerTests(unittest.TestCase):
    MONITOR = {"index": 2, "left": 1920, "top": 0, "width": 1920, "height": 1080}

    def make(self, seen, drawn=None):
        calls = []

        def render(handle):
            calls.append(handle)
            return renders(2, 2, {(0, 0): (255, 255, 255, 1.0)}) + (0, 0)

        return pointer.Pointer(self.MONITOR, read=lambda: seen[0], render=drawn or render), calls

    def test_the_position_is_given_inside_the_monitor(self):
        p, _ = self.make([(77, 2000, 100)])
        self.assertEqual(p.where(), (77, 80, 100))

    def test_a_pointer_on_another_monitor_or_hidden_is_not_drawn(self):
        p, _ = self.make([(77, 100, 100)])
        self.assertIsNone(p.where())
        p, _ = self.make([None])
        self.assertIsNone(p.where())

    def test_a_pointer_picture_is_made_once_and_kept(self):
        p, calls = self.make([(5, 2000, 10)])
        frame = solid(1920, 1080, (0, 0, 0))
        p.draw(frame, p.where())
        p.draw(frame, p.where())
        self.assertEqual(calls, [5])
        self.assertEqual(tuple(frame[10, 80, :3]), (255, 255, 255))

    def test_a_pointer_that_cannot_be_drawn_leaves_the_frame_alone(self):
        p, _ = self.make([(5, 2000, 10)], drawn=lambda handle: None)
        frame = solid(1920, 1080, (3, 3, 3))
        self.assertIsNone(p.draw(frame, p.where()))
        self.assertEqual(tuple(frame[10, 80, :3]), (3, 3, 3))

    def test_the_kept_pictures_are_limited(self):
        p, _ = self.make([None])
        for handle in range(pointer.SPRITE_CACHE_LIMIT + 5):
            p._sprite(handle)
        self.assertLessEqual(len(p._sprites), pointer.SPRITE_CACHE_LIMIT)

    @unittest.skipUnless(sys.platform == "win32", "Windows draws the pointers")
    def test_windows_arrow_comes_out_with_a_solid_body_and_clear_corners(self):
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        handle = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        rendered = pointer.render_pointer(handle)
        self.assertIsNotNone(rendered)
        color, see_through, invert, hot_x, hot_y = pointer.sprite_from_renders(*rendered)
        self.assertGreater(float((see_through < 0.1).sum()), 20, "some solid pixels")
        self.assertGreater(float((see_through > 0.9).sum()), 100, "some clear pixels")
        self.assertEqual(see_through[color.shape[0] - 1, color.shape[1] - 1, 0], 1.0, "the far corner is clear")
        self.assertLess(hot_x, 8)
        self.assertLess(hot_y, 8)

    @unittest.skipUnless(sys.platform == "win32", "Windows tells where the pointer is")
    def test_windows_says_where_the_pointer_is(self):
        seen = pointer.read_pointer()
        if seen is not None:
            self.assertEqual(len(seen), 3)


class FakeCamera:
    def __init__(self, shots):
        self.queue = list(shots)

    def grab(self):
        return self.queue.pop(0) if self.queue else None

    def release(self):
        pass


@unittest.skipIf(numpy is None, "numpy is not installed")
class PipelineTests(unittest.TestCase):
    def pointer_at(self, position):
        render = lambda handle: renders(8, 8, {(r, c): (255, 255, 255, 1.0) for r in range(8) for c in range(8)}) + (0, 0)
        return pointer.Pointer({"left": 0, "top": 0, "width": 64, "height": 36},
                               read=lambda: (1, *position[0]) if position[0] else None, render=render)

    def decode(self, jpeg):
        import io

        from PIL import Image

        return Image.open(io.BytesIO(jpeg)).convert("RGB")

    def test_a_frame_has_the_pointer_on_it(self):
        position = [(20, 10)]
        stream = frames._dxgi_frames(FakeCamera([solid(64, 36, (0, 0, 0))]), 100, None, self.pointer_at(position))
        image = self.decode(next(stream))
        stream.close()
        self.assertGreater(image.getpixel((22, 12))[0], 200)
        self.assertLess(image.getpixel((40, 30))[0], 40)

    def test_moving_over_a_still_screen_sends_a_new_frame_with_the_pointer_in_its_new_place(self):
        position = [(4, 4)]
        stream = frames._dxgi_frames(FakeCamera([solid(64, 36, (0, 0, 0))]), 100, None, self.pointer_at(position))
        first = self.decode(next(stream))
        position[0] = (40, 20)
        second = self.decode(next(stream))
        stream.close()
        self.assertGreater(first.getpixel((6, 6))[0], 200)
        self.assertLess(second.getpixel((6, 6))[0], 40, "gone from the old place")
        self.assertGreater(second.getpixel((42, 22))[0], 200, "drawn at the new place")

    def test_without_a_pointer_the_frame_is_the_screen_alone(self):
        stream = frames._dxgi_frames(FakeCamera([solid(64, 36, (0, 0, 0))]), 100, None, None)
        image = self.decode(next(stream))
        stream.close()
        self.assertLess(image.getpixel((22, 12))[0], 40)

    def test_a_still_pointer_on_a_still_screen_repeats_the_frame_and_does_not_redraw_it(self):
        position = [(4, 4)]
        with mock.patch.object(frames, "STATIC_REPEAT", 0.05):
            stream = frames._dxgi_frames(FakeCamera([solid(64, 36, (0, 0, 0))]), 100, None, self.pointer_at(position))
            first = next(stream)
            second = next(stream)
            stream.close()
        self.assertEqual(first, second)

    def test_the_grabber_leaves_the_pointer_out_when_asked(self):
        seen = []
        with mock.patch.object(frames, "_open_dxgi", return_value=None), \
                mock.patch.object(frames, "_pointer_for", return_value="a pointer"), \
                mock.patch.object(frames, "_mss_frames", side_effect=lambda m, f, h, pointer=None: seen.append(pointer) or iter([b"x"])):
            next(frames.ScreenGrabber(1).frames(30, None, pointer=False))
            next(frames.ScreenGrabber(1).frames(30, None))
        self.assertEqual(seen, [None, "a pointer"])


if __name__ == "__main__":
    unittest.main()
