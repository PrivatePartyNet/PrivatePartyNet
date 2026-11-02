"""The screen capture: encoding, matching a monitor to a GPU output, and the Desktop Duplication pipeline with a fake
camera. None of this touches the real screen; tests\\test_frames.py and the build's smoke test cover the real thing."""

import io
import sys
import threading
import time
import types
import unittest
from unittest import mock

from PIL import Image

from privatepartynet import frames

try:
    import numpy
except ImportError:  # the capture packages are optional at run time; these tests need them
    numpy = None

try:
    import cv2
except ImportError:
    cv2 = None


def screen(width, height, color=(200, 90, 30)):
    """A fake dxcam frame: height x width x 4 BGRA."""
    array = numpy.zeros((height, width, 4), dtype=numpy.uint8)
    array[:, :, 0], array[:, :, 1], array[:, :, 2], array[:, :, 3] = color[2], color[1], color[0], 255
    return array


class FakeCamera:
    """Hands out frames from a list, then nothing (a screen that stopped changing), or raises."""

    def __init__(self, frames_to_give=(), then_none=True, error=None):
        self.queue = list(frames_to_give)
        self.then_none = then_none
        self.error = error
        self.released = False

    def grab(self):
        if self.error is not None:
            raise self.error
        if self.queue:
            return self.queue.pop(0)
        return None if self.then_none else screen(64, 36)

    def release(self):
        self.released = True


def size_of(jpeg):
    return Image.open(io.BytesIO(jpeg)).size


@unittest.skipIf(numpy is None, "numpy is not installed")
class EncodeTests(unittest.TestCase):
    def encoders(self):
        yield "Pillow", None
        if cv2 is not None:
            yield "OpenCV", cv2

    def test_a_frame_comes_out_as_a_jpeg_of_the_same_size_with_either_encoder(self):
        for name, backend in self.encoders():
            with mock.patch.object(frames, "_cv2", backend):
                jpeg = frames.encode_bgra(screen(640, 360), None)
                self.assertTrue(jpeg.startswith(b"\xff\xd8"), name)
                self.assertEqual(size_of(jpeg), (640, 360), name)

    def test_the_colors_survive_the_channel_order(self):
        for name, backend in self.encoders():
            with mock.patch.object(frames, "_cv2", backend):
                pixel = Image.open(io.BytesIO(frames.encode_bgra(screen(64, 64, (200, 90, 30)), None))).convert("RGB").getpixel((32, 32))
                self.assertLess(abs(pixel[0] - 200), 12, f"{name}: red")
                self.assertLess(abs(pixel[1] - 90), 12, f"{name}: green")
                self.assertLess(abs(pixel[2] - 30), 12, f"{name}: blue")

    @unittest.skipIf(cv2 is None, "OpenCV is not installed")
    def test_with_opencv_the_ceiling_is_met_exactly(self):
        with mock.patch.object(frames, "_cv2", cv2):
            self.assertEqual(size_of(frames.encode_bgra(screen(3440, 1440), 1080)), (2580, 1080))
            self.assertEqual(size_of(frames.encode_bgra(screen(1920, 1080), 720)), (1280, 720))
            self.assertEqual(size_of(frames.encode_bgra(screen(3440, 1440), 480)), (1147, 480))

    def test_with_pillow_only_whole_number_reductions_are_made(self):
        with mock.patch.object(frames, "_cv2", None):
            self.assertEqual(size_of(frames.encode_bgra(screen(1920, 1080), 720)), (1920, 1080), "a fractional resize is left to WebRTC")
            self.assertEqual(size_of(frames.encode_bgra(screen(1920, 1080), 540)), (960, 540))
            self.assertEqual(size_of(frames.encode_bgra(screen(1920, 1080), 360)), (640, 360))

    def test_no_ceiling_keeps_the_source_size(self):
        for name, backend in self.encoders():
            with mock.patch.object(frames, "_cv2", backend):
                self.assertEqual(size_of(frames.encode_bgra(screen(800, 450), None)), (800, 450), name)
                self.assertEqual(size_of(frames.encode_bgra(screen(800, 450), 2160)), (800, 450), name)

    def test_a_ceiling_above_the_source_does_nothing(self):
        for name, backend in self.encoders():
            with mock.patch.object(frames, "_cv2", backend):
                self.assertEqual(size_of(frames.encode_bgra(screen(800, 450), 1080)), (800, 450), name)


class MonitorOrderTests(unittest.TestCase):
    """Windows may list the same screens in a different order on a later call; a screen's number must not follow that."""

    def listing(self, order):
        screens = {
            "a": {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            "b": {"left": 0, "top": 0, "width": 3440, "height": 1440},
            "c": {"left": 3440, "top": 0, "width": 3840, "height": 2160},
        }
        everything = {"left": -1920, "top": 0, "width": 9200, "height": 2160}
        fake = types.ModuleType("mss")

        class Fake:
            monitors = [everything] + [screens[k] for k in order]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake.MSS = Fake
        with mock.patch.dict(sys.modules, {"mss": fake}):
            return frames.list_monitors()

    def test_screens_are_numbered_left_to_right_whatever_order_windows_lists_them(self):
        first = self.listing("abc")
        for order in ("cba", "bca", "bac", "acb"):
            self.assertEqual(self.listing(order), first, order)
        self.assertEqual([(m["index"], m["width"]) for m in first], [(1, 1920), (2, 3440), (3, 3840)])

    def test_the_main_screen_is_still_the_one_at_the_origin(self):
        self.assertEqual([m["index"] for m in self.listing("cab") if m["primary"]], [2])

    def test_a_screen_is_grabbed_by_its_place_on_the_desktop(self):
        with mock.patch.object(frames, "list_monitors", return_value=[{"index": 2, "left": 0, "top": 0, "width": 3440, "height": 1440, "primary": True}]):
            self.assertEqual(frames._region(2), {"left": 0, "top": 0, "width": 3440, "height": 1440})
            self.assertIsNone(frames._region(7))


class MatchOutputTests(unittest.TestCase):
    OUTPUTS = [(0, 0, 0, 0, 3440, 1440), (0, 1, -1920, 0, 1920, 1080), (0, 2, 3440, 0, 3840, 2160)]

    def test_a_monitor_is_matched_by_its_place_on_the_desktop(self):
        self.assertEqual(frames.match_output({"left": 0, "top": 0, "width": 3440, "height": 1440}, self.OUTPUTS), (0, 0))
        self.assertEqual(frames.match_output({"left": -1920, "top": 0, "width": 1920, "height": 1080}, self.OUTPUTS), (0, 1))
        self.assertEqual(frames.match_output({"left": 3440, "top": 0, "width": 3840, "height": 2160}, self.OUTPUTS), (0, 2))

    def test_two_monitors_of_one_size_are_told_apart_by_position(self):
        twins = [(0, 0, 0, 0, 1920, 1080), (1, 0, 1920, 0, 1920, 1080)]
        self.assertEqual(frames.match_output({"left": 1920, "top": 0, "width": 1920, "height": 1080}, twins), (1, 0))

    def test_a_monitor_with_no_output_matches_nothing(self):
        self.assertIsNone(frames.match_output({"left": 5, "top": 5, "width": 800, "height": 600}, self.OUTPUTS))
        self.assertIsNone(frames.match_output({"left": 0, "top": 0, "width": 800, "height": 600}, self.OUTPUTS))


@unittest.skipIf(numpy is None, "numpy is not installed")
class PipelineTests(unittest.TestCase):
    def test_frames_come_out_in_order_as_jpegs_and_the_threads_stop(self):
        camera = FakeCamera([screen(320, 180, (255, 0, 0)), screen(320, 180, (0, 255, 0)), screen(320, 180, (0, 0, 255))])
        stream = frames._dxgi_frames(camera, fps=200, max_height=None)
        got = [next(stream) for _ in range(3)]
        stream.close()
        self.assertTrue(all(g.startswith(b"\xff\xd8") for g in got))
        reds = [Image.open(io.BytesIO(g)).convert("RGB").getpixel((10, 10)) for g in got]
        self.assertGreater(reds[0][0], 200)
        self.assertGreater(reds[1][1], 200)
        self.assertGreater(reds[2][2], 200)
        time.sleep(0.2)
        self.assertFalse([t for t in threading.enumerate() if t.name == "privatepartynet-dxgi-grab"], "the grabbing thread stopped")

    def test_a_still_screen_repeats_its_last_frame_so_a_new_viewer_is_not_left_blank(self):
        camera = FakeCamera([screen(160, 90)], then_none=True)
        with mock.patch.object(frames, "STATIC_REPEAT", 0.05):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None)
            first = next(stream)
            started = time.perf_counter()
            second = next(stream)
            third = next(stream)
            stream.close()
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertLess(time.perf_counter() - started, 1.5)

    def test_no_first_frame_is_reported_so_the_other_capture_can_take_over(self):
        camera = FakeCamera([], then_none=True)
        with mock.patch.object(frames, "FIRST_FRAME_WAIT", 0.2):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None)
            with self.assertRaises(RuntimeError):
                next(stream)
        stream.close()

    def test_a_screen_that_never_changes_still_starts_from_a_seeded_first_frame(self):
        camera = FakeCamera([], then_none=True)
        with mock.patch.object(frames, "SEED_WAIT", 0.1), mock.patch.object(frames, "FIRST_FRAME_WAIT", 1.0):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None, seed=lambda: screen(96, 54, (10, 200, 10)))
            first = next(stream)
            stream.close()
        self.assertEqual(size_of(first), (96, 54))

    def test_a_silent_gpu_stream_on_a_screen_that_changes_hands_over_to_the_fallback(self):
        camera = FakeCamera([], then_none=True)  # Desktop Duplication sends nothing at all
        counter = {"n": 0}

        def changing_screen():
            counter["n"] += 1
            return screen(96, 54, (30 * (counter["n"] % 8), 200, 20))  # a different picture on every look

        with mock.patch.object(frames, "SEED_WAIT", 0.1), mock.patch.object(frames, "STALL_CHECK", 0.3), mock.patch.object(frames, "FIRST_FRAME_WAIT", 30.0):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None, seed=changing_screen)
            started = time.perf_counter()
            with self.assertRaises(RuntimeError) as caught:
                while True:
                    next(stream)
            self.assertIn("screen changes", str(caught.exception))
            self.assertLess(time.perf_counter() - started, 5.0)
        stream.close()

    def test_a_silent_gpu_stream_on_a_screen_that_is_really_still_keeps_going(self):
        camera = FakeCamera([], then_none=True)
        with mock.patch.object(frames, "SEED_WAIT", 0.1), mock.patch.object(frames, "STALL_CHECK", 0.2), mock.patch.object(frames, "STATIC_REPEAT", 0.05):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None, seed=lambda: screen(96, 54, (10, 200, 10)))
            started = time.perf_counter()
            got = 0
            while time.perf_counter() - started < 1.5:
                next(stream)
                got += 1
            stream.close()
        self.assertGreater(got, 5, "it went on repeating the still frame past several checks")

    def test_two_screens_differ_only_when_a_real_share_of_them_changed(self):
        base = screen(480, 270, (50, 50, 50))
        pointer_sized = base.copy()
        pointer_sized[100:112, 200:212] = 255  # a pointer-sized patch
        self.assertFalse(frames.screens_differ(base, pointer_sized))
        moved = base.copy()
        moved[:, 240:] = 200  # half the screen
        self.assertTrue(frames.screens_differ(base, moved))
        self.assertFalse(frames.screens_differ(base, screen(96, 54)))
        self.assertFalse(frames.screens_differ(None, base))

    def test_a_seed_that_fails_still_ends_in_the_fallback(self):
        camera = FakeCamera([], then_none=True)
        with mock.patch.object(frames, "SEED_WAIT", 0.1), mock.patch.object(frames, "FIRST_FRAME_WAIT", 0.4):
            stream = frames._dxgi_frames(camera, fps=100, max_height=None, seed=lambda: None)
            with self.assertRaises(RuntimeError):
                next(stream)
        stream.close()

    def test_a_failure_in_the_grabbing_thread_reaches_the_caller(self):
        camera = FakeCamera(error=OSError("access lost"))
        stream = frames._dxgi_frames(camera, fps=100, max_height=None)
        with self.assertRaises(OSError):
            next(stream)
        stream.close()

    def test_the_pace_follows_the_requested_rate_and_never_runs_far_ahead(self):
        camera = FakeCamera(then_none=False)  # a new frame at every grab
        stream = frames._dxgi_frames(camera, fps=30, max_height=None)
        started = time.perf_counter()
        count = 0
        while time.perf_counter() - started < 1.0:
            next(stream)
            count += 1
        stream.close()
        self.assertGreater(count, 18)
        self.assertLess(count, 40)


class GrabberTests(unittest.TestCase):
    def test_without_desktop_duplication_the_fallback_capture_is_used(self):
        with mock.patch.object(frames, "_open_dxgi", return_value=None), \
                mock.patch.object(frames, "_mss_frames", side_effect=lambda monitor, fps, height, pointer=None: iter([b"mss-frame"])):
            grabber = frames.ScreenGrabber(2)
            self.assertEqual(next(grabber.frames(30, None)), b"mss-frame")
            self.assertEqual(grabber.backend, "mss")

    def test_when_the_gpu_path_fails_the_camera_is_released_and_the_fallback_takes_over(self):
        camera = FakeCamera()
        frames._dx_busy.add((0, 0))

        def broken(cam, fps, height, pointer=None):
            raise RuntimeError("no first frame")
            yield b""  # pragma: no cover

        with mock.patch.object(frames, "_open_dxgi", return_value=(camera, (0, 0))), \
                mock.patch.object(frames, "_dxgi_frames", broken), \
                mock.patch.object(frames, "_mss_frames", side_effect=lambda monitor, fps, height, pointer=None: iter([b"fallback"])),                 self.assertLogs("privatepartynet.capture", level="ERROR"):
            grabber = frames.ScreenGrabber(1)
            self.assertEqual(next(grabber.frames(30, None)), b"fallback")
        self.assertTrue(camera.released)
        self.assertNotIn((0, 0), frames._dx_busy)
        self.assertEqual(grabber.backend, "mss")

    def test_closing_the_stream_releases_the_output(self):
        camera = FakeCamera([screen(64, 36)] * 5)
        frames._dx_busy.add((0, 3))
        with mock.patch.object(frames, "_open_dxgi", return_value=(camera, (0, 3))):
            grabber = frames.ScreenGrabber(1)
            stream = grabber.frames(100, None)
            next(stream)
            self.assertEqual(grabber.backend, "dxgi")
            stream.close()
        self.assertTrue(camera.released)
        self.assertNotIn((0, 3), frames._dx_busy)


class OpenTests(unittest.TestCase):
    def fake_dxcam(self, create):
        module = types.ModuleType("dxcam")
        module.create = create
        rect = types.SimpleNamespace(left=0, top=0, right=1920, bottom=1080)
        factory = types.SimpleNamespace(outputs=[[types.SimpleNamespace(desc=types.SimpleNamespace(DesktopCoordinates=rect))]])
        setattr(module, "__factory", factory)  # a plain assignment would be name-mangled inside a class
        return module

    def monitors(self):
        return [{"index": 1, "width": 1920, "height": 1080, "left": 0, "top": 0, "primary": True}]

    def test_an_output_already_in_use_is_not_opened_twice(self):
        camera = object()
        module = self.fake_dxcam(lambda **kwargs: camera)
        frames._dx_busy.discard((0, 0))
        with mock.patch.dict(sys.modules, {"dxcam": module}), mock.patch.object(frames, "list_monitors", self.monitors):
            first = frames._open_dxgi(1)
            self.assertEqual(first, (camera, (0, 0)))
            with mock.patch.object(frames, "OPEN_RETRY_SECONDS", 0.2):
                self.assertIsNone(frames._open_dxgi(1), "a second stream on the same output uses the fallback")
            frames._close_dxgi(types.SimpleNamespace(release=lambda: None), (0, 0))
            self.assertEqual(frames._open_dxgi(1), (camera, (0, 0)), "free again once released")
            frames._close_dxgi(types.SimpleNamespace(release=lambda: None), (0, 0))

    def test_a_restarting_share_waits_for_the_old_stream_to_let_go_of_the_output(self):
        camera = object()
        module = self.fake_dxcam(lambda **kwargs: camera)
        frames._dx_busy.add((0, 0))  # the old stream still has it
        threading.Timer(0.3, lambda: frames._dx_busy.discard((0, 0))).start()  # and lets go a moment later
        started = time.monotonic()
        with mock.patch.dict(sys.modules, {"dxcam": module}), mock.patch.object(frames, "list_monitors", self.monitors):
            self.assertEqual(frames._open_dxgi(1), (camera, (0, 0)))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        frames._close_dxgi(types.SimpleNamespace(release=lambda: None), (0, 0))

    def test_a_camera_that_cannot_be_created_gives_up_and_frees_the_output(self):
        def refuse(**kwargs):
            raise RuntimeError("busy")

        module = self.fake_dxcam(refuse)
        frames._dx_busy.discard((0, 0))
        with mock.patch.dict(sys.modules, {"dxcam": module}), mock.patch.object(frames, "list_monitors", self.monitors), \
                mock.patch.object(frames, "OPEN_RETRY_SECONDS", 0.2):
            self.assertIsNone(frames._open_dxgi(1))
        self.assertNotIn((0, 0), frames._dx_busy)

    def test_a_package_that_is_not_installed_falls_back_quietly(self):
        with mock.patch.dict(sys.modules, {"dxcam": None}):
            self.assertIsNone(frames._open_dxgi(1))


if __name__ == "__main__":
    unittest.main()
