import io
import unittest

from PIL import Image

from privatepartynet.frames import clamp_fps, clamp_height, encode_jpeg


class FakeShot:
    """What mss hands back: the size, packed RGB, and packed BGRA."""

    def __init__(self, width, height):
        self.size = (width, height)
        self.width = width
        self.height = height
        self.rgb = bytes([30, 90, 200]) * (width * height)
        self.bgra = bytes([200, 90, 30, 255]) * (width * height)


class FrameTests(unittest.TestCase):
    def test_a_tall_screen_is_shrunk_to_the_ceiling(self):
        jpeg = encode_jpeg(FakeShot(1920, 1080), max_height=720)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (1280, 720))

    def test_no_ceiling_keeps_the_source_size(self):
        self.assertEqual(Image.open(io.BytesIO(encode_jpeg(FakeShot(640, 360), max_height=None))).size, (640, 360))

    def test_requests_are_clamped_to_sane_values(self):
        self.assertEqual(clamp_fps("60"), 60)
        self.assertEqual(clamp_fps("29"), 30)
        self.assertEqual(clamp_fps(None), 30)
        self.assertEqual(clamp_height("1080"), 1080)
        self.assertIsNone(clamp_height("99999"))
        self.assertIsNone(clamp_height("abc"))


class FeedTests(unittest.TestCase):
    def setUp(self):
        from privatepartynet.feed import FeedServer

        class Grabber:
            def frames(self, fps, max_height, pointer=True):
                yield b"frame-a"
                yield b"frame-b"

            def monitors(self):
                return [{"index": 1, "width": 1920, "height": 1080, "primary": True}]

            def for_monitor(self, monitor):
                return self

        self.feed = FeedServer(Grabber())
        self.feed.start()
        self.addCleanup(self.feed.stop)
        self.token = self.feed.issue()

    def request(self, method, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.feed.port, timeout=5)
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_the_feed_refuses_anyone_without_the_token(self):
        response, _ = self.request("GET", "/frames?fps=30")
        self.assertEqual(response.status, 403)

    def test_the_feed_answers_the_preflight_for_private_network_access(self):
        response, _ = self.request("OPTIONS", "/frames")
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Private-Network"), "true")
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")

    def test_the_feed_streams_frames_and_lists_screens_for_the_right_token(self):
        response, body = self.request("GET", f"/monitors?token={self.token}")
        self.assertEqual(response.status, 200)
        self.assertIn(b"primary", body)
        response, body = self.request("GET", f"/frames?fps=30&token={self.token}&monitor=1")
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"\x00\x00\x00\x07frame-a\x00\x00\x00\x07frame-b")

    def test_the_feed_reports_this_apps_cpu_only_for_its_token(self):
        response, _ = self.request("GET", "/cpu")
        self.assertEqual(response.status, 403)
        response, body = self.request("GET", f"/cpu?token={self.token}")
        self.assertEqual(response.status, 200)
        self.assertIn(b'"app"', body)

    def test_a_new_join_replaces_the_old_token(self):
        old = self.token
        self.feed.issue()
        response, _ = self.request("GET", f"/monitors?token={old}")
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
