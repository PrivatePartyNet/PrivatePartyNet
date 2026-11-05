"""The one window, from the app's side: what its rail is told to open and close, the calls a room in a frame may make
(and only for itself), About and How over the window, one fullscreen for every room, and older hosts in a window of
their own."""

import socket
import tempfile
import unittest
from unittest import mock

from privatepartynet import app
from privatepartynet.app import ROOM_CALLS, Controller, WindowApi


class FakeGrabber:
    def frames(self, fps, max_height, pointer=True):
        while True:
            yield b"\xff\xd8fake-frame\xff\xd9"


class FakeShell:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeWindow:
    def __init__(self):
        self.toggles = 0
        self.events = type("Events", (), {"closed": type("Hook", (), {"__iadd__": lambda self, fn: self})()})()

    def toggle_fullscreen(self):
        self.toggles += 1

    def destroy(self):
        raise AssertionError("the one window is never closed for a room")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def no_windows(*args, **kwargs):
    raise AssertionError("nothing opens a window of its own in the one window")


class ShellTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.controller = Controller(data_folder=folder.name, discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.controller.shutdown)
        self.shell = FakeShell()
        self.controller.use_shell(self.shell)
        self.window = FakeWindow()
        self.controller.bind_window(self.window)
        patcher = mock.patch.object(app.webview, "create_window", no_windows)
        patcher.start()
        self.addCleanup(patcher.stop)

    def events(self):
        return self.controller.shell_state()["events"]

    def test_a_room_may_call_exactly_what_a_party_window_may(self):
        public = {name for name in dir(WindowApi) if not name.startswith("_")}
        self.assertEqual(set(ROOM_CALLS), public)

    def test_your_party_is_on_the_rail_while_it_runs(self):
        self.assertTrue(self.controller.start("Desk", free_port())["ok"])
        (opened,) = self.events()
        self.assertEqual((opened["type"], opened["key"], opened["own"], opened["select"]), ("open", "own", True, True),
                         "starting your party opens its room and shows it")
        self.assertTrue(opened["url"].startswith("http://127.0.0.1:") and opened["url"].endswith("/host"))
        self.assertEqual(self.events(), [], "each event is handed over once")
        self.controller.open_room()
        self.assertEqual([(e["key"], e["select"]) for e in self.events()], [("own", True)], "Open party room shows it")
        self.controller.stop()
        self.assertEqual(self.events(), [{"type": "close", "key": "own"}])
        self.assertEqual(self.controller.room_call("own", "appearance", []), {"ok": False, "message": "That cannot be done here."})

    def test_a_room_asks_only_for_itself_and_only_what_is_allowed(self):
        self.controller.start("Desk", free_port())
        self.events()
        look = self.controller.room_call("own", "appearance", [])
        self.assertEqual(set(look), {"accent", "text_scale", "density"})
        refused = {"ok": False, "message": "That cannot be done here."}
        for key, method, args in (("nobody", "appearance", []), ("own", "shutdown", []), ("own", "_close_children", []),
                                  ("own", "appearance", "x"), ("own", "keep_blob", [1] * 7), (None, "appearance", [])):
            with self.subTest(key=key, method=method):
                self.assertEqual(self.controller.room_call(key, method, args), refused)
        self.assertFalse(self.controller.room_call("own", "keep_chat", [1, 2])["ok"], "a wrong number of arguments is refused")
        self.assertEqual(self.controller.room_call("own", "close_window", []), {"ok": True, "message": ""})
        self.assertEqual(self.events(), [{"type": "close", "key": "own"}], "leaving takes it off the rail")

    def test_a_host_that_can_open_in_a_frame_joins_on_the_rail(self):
        url = "https://192.168.1.20:8765/join?token=abc"
        self.controller._join({"url": url, "name": "Kay's party", "pins": ("a" * 64,), "embed": True})
        (opened,) = self.events()
        self.assertEqual((opened["key"], opened["name"], opened["own"]), ("join:https://192.168.1.20:8765", "Kay's party", False))
        self.assertIn("token=abc", opened["url"])
        self.assertIn("feed_token=", opened["url"], "the member's own screen feed comes along")
        self.assertIn("join:https://192.168.1.20:8765", self.controller._party_windows, "an update waits for the call")
        self.assertTrue(self.controller._update_busy())
        self.controller.room_call("join:https://192.168.1.20:8765", "close_window", [])
        self.assertEqual(self.controller._party_windows, set())

    def test_an_older_host_opens_in_a_window_of_its_own(self):
        made = []
        with mock.patch.object(app.webview, "create_window", lambda *a, **k: made.append(k["url"]) or FakeWindow()):
            self.controller._join({"url": "https://192.168.1.20:8765/join?token=abc", "name": "Old", "pins": ("a" * 64,)})
        self.assertEqual(len(made), 1)
        self.assertEqual(self.events(), [])

    def test_about_how_and_messages_open_in_the_window(self):
        self.controller.open_about()
        self.controller.open_how()
        self.controller.open_messages()
        self.assertEqual([(e["type"], e.get("page")) for e in self.events()], [("info", "about"), ("info", "how"), ("home", "messages")])

    def test_fullscreen_is_the_whole_windows_and_ends_with_its_room(self):
        self.controller.start("Desk", free_port())
        self.assertEqual(self.controller.room_call("own", "set_fullscreen", [True]), {"ok": True, "fullscreen": True})
        self.controller.room_call("own", "set_fullscreen", [True])
        self.assertEqual(self.window.toggles, 1, "asking again for what is already so does nothing")
        self.controller.room_call("own", "close_window", [])
        self.assertEqual(self.window.toggles, 2, "a room that goes takes fullscreen with it")

    def test_a_refused_certificate_is_shown_in_the_rooms_place(self):
        self.window.ppn_refused("https://192.168.1.20:8765")
        self.assertEqual(self.events(), [{"type": "refused", "origin": "https://192.168.1.20:8765"}])

    def test_the_rail_learns_the_theme_look_and_unread_messages(self):
        state = self.controller.shell_state()
        self.assertEqual(state["theme"], "dark")
        self.assertEqual(set(state["appearance"]), {"accent", "text_scale", "density"})
        self.assertEqual(state["dm_unread"], 0)
        self.assertFalse(state["running"])

    def test_closing_the_app_stops_the_page_server(self):
        self.controller.shutdown()
        self.assertTrue(self.shell.stopped)


if __name__ == "__main__":
    unittest.main()
