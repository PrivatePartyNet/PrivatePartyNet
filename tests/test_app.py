import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet import app
from privatepartynet.app import Controller, WindowApi, browser_arguments, webview_options
from privatepartynet.settings import load_settings


class FakeGrabber:
    """Stands in for the screen: frames() yields small byte strings, so no real screen is needed."""

    def frames(self, fps, max_height, pointer=True):
        while True:
            yield b"\xff\xd8fake-frame\xff\xd9"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeWindow:
    """Stands in for a pywebview window: records what the app asked of it."""

    def __init__(self):
        self.toggles = 0
        self.destroyed = False
        self.events = type("Events", (), {"closed": type("Hook", (), {"__iadd__": lambda self, fn: self})()})()

    def toggle_fullscreen(self):
        self.toggles += 1

    def destroy(self):
        self.destroyed = True


class WindowApiTests(unittest.TestCase):
    def make(self):
        api = WindowApi("http://127.0.0.1:1")
        window = FakeWindow()
        api._window = window
        return api, window

    def test_fullscreen_turns_the_window_on_once_and_off_once(self):
        api, window = self.make()
        self.assertEqual(api.set_fullscreen(True), {"ok": True, "fullscreen": True})
        self.assertEqual(api.set_fullscreen(True), {"ok": True, "fullscreen": True})
        self.assertEqual(window.toggles, 1, "asking again for what is already so does nothing")
        self.assertEqual(api.set_fullscreen(False), {"ok": True, "fullscreen": False})
        self.assertEqual(window.toggles, 2)
        api.set_fullscreen(False)
        self.assertEqual(window.toggles, 2)

    def test_fullscreen_without_a_window_says_so(self):
        self.assertEqual(WindowApi("http://127.0.0.1:1").set_fullscreen(True), {"ok": False, "fullscreen": False})

    def test_what_the_page_may_call_is_only_what_is_meant(self):
        public = {name for name in dir(WindowApi) if not name.startswith("_")}
        self.assertEqual(public, {"open_viewer", "close_window", "set_fullscreen", "keep_chat", "keep_blob"})

    def test_closing_the_window_destroys_it(self):
        api, window = self.make()
        self.assertTrue(api.close_window()["ok"])
        self.assertTrue(window.destroyed)


class WebviewOptionsTests(unittest.TestCase):
    """pywebview's private mode deletes every window's cookies each time a window is created, which signed the open party
    windows out whenever a pop-out (or any window) opened. The app must start with a normal profile."""

    def test_the_profile_is_a_normal_one_in_the_data_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            options = webview_options(folder + "/webview")
            self.assertIs(options["private_mode"], False)
            self.assertTrue(options["storage_path"].endswith("webview"))
            self.assertTrue(__import__("os").path.isdir(options["storage_path"]))

    def test_certificate_checks_are_never_turned_off_for_every_window(self):
        # Party windows accept the one certificate their join checked (windowpins.py). The flag that accepted any
        # certificate at all is taken out, even when something else on the PC put it in the variable, and the
        # variable's other arguments are kept.
        flag = "--ignore-certificate-errors"
        self.assertEqual(browser_arguments(None), "")
        self.assertEqual(browser_arguments(flag), "")
        self.assertEqual(browser_arguments("--disable-gpu"), "--disable-gpu")
        self.assertEqual(browser_arguments(f"--disable-gpu {flag} --lang=en"), "--disable-gpu --lang=en")
        source = (Path(app.__file__)).read_text(encoding="utf-8")
        self.assertNotIn('setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"', source)
        main = source[source.index("def main("):]
        self.assertIn("browser_arguments(", main)
        self.assertLess(main.index("windowpins.install()"), main.index("webview.start("), "before any window opens")

    def test_the_app_ends_at_once_after_its_windows_close(self):
        # pywebview answers a page's last bridge calls from threads of its own; once the window stack has stopped,
        # such an answer reaching into .NET while Python shuts down crashed the process (an access violation, exit 127).
        from unittest import mock

        with mock.patch.object(app.os, "_exit") as end, mock.patch.object(app.logging, "shutdown") as flush:
            app.exit_now(3)
        flush.assert_called_once_with()
        end.assert_called_once_with(3)
        source = (Path(app.__file__)).read_text(encoding="utf-8")
        main = source[source.index("def main("):]
        self.assertIn("exit_now(window_test())", main)
        self.assertLess(main.index("webview.start("), main.index("exit_now(0)"))

    def test_every_start_of_the_window_stack_uses_those_options(self):
        import re
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "privatepartynet" / "app.py").read_text(encoding="utf-8")
        starts = re.findall(r"webview\.start\((.*)\)", source)
        self.assertGreaterEqual(len(starts), 2)
        for call in starts:
            self.assertIn("**webview_options(", call, call)
        self.assertNotIn("private_mode=True", source)

    def test_pywebview_still_deletes_cookies_in_private_mode_only(self):
        """The reason for the rule above, read from the installed pywebview, so an update that changes it is noticed."""
        import inspect

        from webview.platforms import edgechromium

        source = inspect.getsource(edgechromium)
        marker = "if _state['private_mode']:"
        self.assertIn(marker, source)
        after = source.split(marker, 1)[1][:260]
        self.assertIn("DeleteAllCookies", after)


class ControllerTests(unittest.TestCase):
    """The app's state and actions, without a window. The window itself is checked by the built app's launch test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.controller = Controller(data_folder=self.folder, discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.controller.shutdown)

    def test_about_info_names_the_version_and_where_things_are(self):
        from privatepartynet import APP_NAME, __version__

        info = self.controller.about_info()
        self.assertEqual(info["name"], APP_NAME)
        self.assertEqual(info["version"], __version__)
        self.assertEqual(info["data_folder"], self.folder)
        self.assertTrue(info["log_file"].endswith("privatepartynet.log"))
        self.assertEqual([lib["name"] for lib in info["libraries"]][:2], ["pywebview", "mss"])
        self.assertTrue(all(lib["version"] for lib in info["libraries"]))
        self.assertEqual(self.controller.state()["app_name"], APP_NAME)

    def test_a_message_the_panel_hid_is_logged_by_where_never_by_what(self):
        with self.assertLogs("privatepartynet", "WARNING") as logged:
            self.assertEqual(self.controller.hidden_message("join status", "a certificate or key"), {"ok": True})
        self.assertIn("join status", logged.output[0])
        self.assertIn("a certificate or key", logged.output[0])
        with self.assertLogs("privatepartynet", "WARNING") as logged:
            self.controller.hidden_message("-----BEGIN CERTIFICATE-----\nMIID", "x" * 500)
        self.assertNotIn("BEGIN", logged.output[0], "only a short plain label reaches the log")
        self.assertLess(len(logged.output[0]), 300)

    def test_every_status_box_in_the_control_panel_goes_through_the_guard(self):
        source = (Path(app.__file__).parent / "web" / "control.js").read_text(encoding="utf-8")
        for unguarded in ("textContent = result.message", "textContent = status.message",
                          "textContent = game.message", "textContent = state.join.message",
                          "textContent = reach.advice", "notice.textContent = text"):
            self.assertFalse(unguarded in source, unguarded)
        html = (Path(app.__file__).parent / "web" / "control.html").read_text(encoding="utf-8")
        self.assertLess(html.index("safetext.js"), html.index("control.js"))

    def test_the_game_network_line_says_each_members_way_and_latency(self):
        class Paths:
            names = {"u-alex": "Alex", "u-bea": "Bea", "u-cy": "Cy"}

            def state(self):
                return {"through": [], "peers": {"u-alex": {"kind": "ipv6", "rtt": 23, "ways": ["ipv6"]},
                                                 "u-bea": {"kind": "friend", "rtt": 160, "ways": ["friend"]},
                                                 "u-cy": {"kind": "", "rtt": None, "ways": []}}}

            def problems(self):
                return []

        self.controller.path_manager = Paths()
        view = self.controller.state()["friend_relay"]
        self.assertEqual(view["members"], [
            {"name": "Alex", "way": "direct over IPv6", "rtt": 23},
            {"name": "Bea", "way": "through a member's PC", "rtt": 160},
            {"name": "Cy", "way": "not connected yet", "rtt": None},
        ])
        self.assertEqual(view["ways"], ["Alex, direct over IPv6, 23 ms", "Bea, through a member's PC, 160 ms",
                                        "Cy, not connected yet"])

    def test_the_in_memory_paths_report_a_latency_too(self):
        from privatepartynet.pathway import LoopbackHub, LoopbackPathManager

        hub = LoopbackHub()
        a, b = LoopbackPathManager("a", hub=hub, kind="ipv6"), LoopbackPathManager("b", hub=hub)
        a.path_to("b", b.local_candidates())
        self.assertEqual(a.state()["peers"]["b"]["kind"], "ipv6")
        self.assertIsInstance(a.state()["peers"]["b"]["rtt"], int)

    def test_state_carries_everything_the_panel_shows(self):
        state = self.controller.state()
        for key in ("version", "theme", "name", "port", "running", "addresses", "passcode", "others", "peers", "requests"):
            self.assertIn(key, state)
        self.assertFalse(state["running"])
        self.assertEqual(state["passcode"], "")

    def test_the_saved_chats_and_the_identity_open_without_a_password(self):
        state = self.controller.state()
        self.assertFalse(state["locked"])
        self.assertFalse(state["chat_password"])
        self.assertEqual(len(state["user_id"]), 32)

    def test_the_page_is_not_handed_the_store_or_the_keys(self):
        public = {name for name in dir(self.controller) if not name.startswith("_")}
        self.assertNotIn("store", public)
        self.assertNotIn("identity", public)
        self.assertNotIn("vault", public)

    def test_a_party_keeps_its_server_id_and_its_chat_between_runs(self):
        port = free_port()
        self.assertTrue(self.controller.start("Desk", port)["ok"])
        first_id = load_settings(self.folder)["server_id"]
        self.assertTrue(first_id)
        party = self.controller._party
        party.chat(party.host_id, "remember this")
        self.controller.stop()
        self.assertTrue(self.controller.start("Desk", port)["ok"])
        self.assertEqual(load_settings(self.folder)["server_id"], first_id)
        history = self.controller._party.channel_history(self.controller._party.host_id, "general")
        self.assertEqual([m["text"] for m in history], ["remember this"])

    def test_a_password_locks_the_chats_until_it_is_given(self):
        self.assertTrue(self.controller.set_chat_password("open sesame")["ok"])
        self.controller.shutdown()
        reopened = Controller(data_folder=self.folder, discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(reopened.shutdown)
        state = reopened.state()
        self.assertTrue(state["locked"])
        self.assertFalse(reopened.start("Desk", free_port())["ok"], "no party while the chats are locked")
        self.assertFalse(reopened.unlock("wrong")["ok"])
        self.assertTrue(reopened.unlock("open sesame")["ok"])
        self.assertFalse(reopened.state()["locked"])
        self.assertTrue(reopened.state()["chat_password"])

    def test_removing_the_password_needs_the_old_one(self):
        self.controller.set_chat_password("one")
        self.assertFalse(self.controller.set_chat_password("", "not one")["ok"])
        self.assertTrue(self.controller.set_chat_password("", "one")["ok"])
        self.assertFalse(self.controller.state()["chat_password"])

    def test_a_bad_port_is_refused_with_the_reason(self):
        result = self.controller.start("Desk", 80)
        self.assertFalse(result["ok"])
        self.assertIn("1024", result["message"])
        self.assertFalse(self.controller.state()["running"])

    def test_start_and_stop_the_party(self):
        port = free_port()
        result = self.controller.start("Desk", port)
        self.assertTrue(result["ok"], result)
        state = self.controller.state()
        self.assertTrue(state["running"])
        self.assertEqual(len(state["passcode"]), 11)  # ten characters in two groups of five
        self.assertTrue(all(address.endswith(f":{port}") for address in state["addresses"]))
        self.assertEqual(load_settings(self.folder)["port"], port)
        self.controller.stop()
        self.assertFalse(self.controller.state()["running"])

    def test_names_are_saved_as_they_change(self):
        self.controller.rename("Kitchen PC")
        self.assertEqual(self.controller.state()["name"], "Kitchen PC")
        self.assertEqual(load_settings(self.folder)["name"], "Kitchen PC")

    def test_the_theme_is_remembered_and_checked(self):
        self.assertEqual(self.controller.state()["theme"], "system")
        self.assertTrue(self.controller.set_theme("dark")["ok"])
        self.assertEqual(self.controller.state()["theme"], "dark")
        self.assertEqual(load_settings(self.folder)["theme"], "dark")
        self.assertFalse(self.controller.set_theme("neon")["ok"])
        self.assertEqual(self.controller.state()["theme"], "dark")

    def test_a_theme_change_reaches_a_running_party(self):
        self.controller.start("Desk", free_port())
        self.controller.set_theme("light")
        self.assertEqual(self.controller._party.theme, "light")

    def test_the_party_room_needs_a_window_and_a_running_party(self):
        self.assertFalse(self.controller.open_room()["ok"])

    def test_an_unknown_join_request_is_refused(self):
        self.assertFalse(self.controller.decide("nobody", True)["ok"])

    def test_a_join_request_is_listed_until_decided(self):
        self.controller._requests["peer-1"] = {"id": "peer-1", "name": "Alex", "address": "192.168.0.9", "at": time.monotonic()}
        self.assertEqual([r["name"] for r in self.controller.state()["requests"]], ["Alex"])
        self.controller.decide("peer-1", False)
        self.assertEqual(self.controller.state()["requests"], [])


if __name__ == "__main__":
    unittest.main()


class BackupThroughTheControllerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.one = Controller(data_folder=self.folder + "/one", discovery_port=0, grabber=FakeGrabber())
        self.two = Controller(data_folder=self.folder + "/two", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.one.shutdown)
        self.addCleanup(self.two.shutdown)

    def test_a_backup_moves_chats_between_installs(self):
        self.one._store.add_message("a", "dm:u2", "u1", {"text": "keep me", "name": "Kay"}, ts=1.0)
        path = self.folder + "/chats.ppnbak"
        result = self.one._export_to(path, "pass phrase", False)
        self.assertTrue(result["ok"])
        self.assertIn("Saved 1 messages", result["message"])
        moved = self.two._import_from(path, "pass phrase", False)
        self.assertTrue(moved["ok"])
        self.assertEqual(self.two._store.history("dm:u2")[0]["body"]["text"], "keep me")

    def test_the_identity_moves_only_when_asked(self):
        self.one._store.add_message("a", "dm:u2", "u1", {"text": "x", "name": "Kay"}, ts=1.0)
        path = self.folder + "/with.ppnbak"
        self.one._export_to(path, "pw", True)
        before = self.two._identity.user_id
        self.two._import_from(path, "pw", False)
        self.assertEqual(self.two._identity.user_id, before)
        self.two._import_from(path, "pw", True)
        self.assertEqual(self.two._identity.user_id, self.one._identity.user_id)
        self.two.shutdown()
        reopened = Controller(data_folder=self.folder + "/two", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(reopened.shutdown)
        self.assertEqual(reopened._identity.user_id, self.one._identity.user_id, "the new identity is what is stored")

    def test_the_dialog_methods_ask_for_a_passphrase_and_survive_a_cancel(self):
        self.assertFalse(self.one.export_chats("")["ok"])
        self.assertEqual(self.one.export_chats("pw")["message"], "Cancelled.", "no window, so no path was chosen")

    def test_a_wrong_passphrase_is_reported_not_raised(self):
        path = self.folder + "/chats.ppnbak"
        self.one._export_to(path, "right", False)
        self.two._ask_path = lambda save, name="": path
        result = self.two.import_chats("wrong")
        self.assertFalse(result["ok"])
        self.assertIn("passphrase", result["message"])


class DirectMessagesThroughTheControllerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.ann = Controller(data_folder=self.folder + "/ann", discovery_port=0, grabber=FakeGrabber())
        self.ben = Controller(data_folder=self.folder + "/ben", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.ann.shutdown)
        self.addCleanup(self.ben.shutdown)
        for who in (self.ann, self.ben):
            who._dm._resolver = lambda user_id: None  # no beacons in tests; the saved address is used
        self.ann._name, self.ben._name = "Ann", "Ben"

    def befriend(self, one, other):
        one._store.save_contact({"id": other._identity.user_id, "name": other._name, "sign": other._identity.sign_public.hex(),
                                 "dh": other._identity.dh_public.hex(), "addr": "127.0.0.1", "port": other._dm.port})

    def test_a_message_goes_from_one_install_to_the_other(self):
        self.befriend(self.ann, self.ben)
        self.assertTrue(self.ann.dm_send(self.ben._identity.user_id, "hello Ben")["ok"])
        self.assertEqual(self.ann._dm.flush(), 1)
        state = self.ben.dm_state()
        (ann,) = state["contacts"]
        self.assertEqual((ann["name"], ann["unread"], ann["new"], ann["preview"]), ("Ann", 1, True, "hello Ben"))
        history = self.ben.dm_history(self.ann._identity.user_id)["messages"]
        self.assertEqual([(m["text"], m["mine"]) for m in history], [("hello Ben", False)])
        self.assertEqual(self.ben.state()["dm_unread"], 1)

    def test_what_you_write_yourself_is_never_unread(self):
        self.befriend(self.ann, self.ben)
        self.ann.dm_send(self.ben._identity.user_id, "to myself, nearly")
        self.assertEqual(self.ann.state()["dm_unread"], 0)
        (ben,) = self.ann.dm_state()["contacts"]
        self.assertEqual(ben["waiting"], 1, "it is still waiting to be delivered")

    def test_reading_clears_the_count(self):
        self.befriend(self.ann, self.ben)
        self.ann.dm_send(self.ben._identity.user_id, "one")
        self.ann._dm.flush()
        self.ben.dm_read(self.ann._identity.user_id)
        self.assertEqual(self.ben.state()["dm_unread"], 0)

    def test_the_safety_number_is_the_same_on_both_sides(self):
        self.befriend(self.ann, self.ben)
        self.befriend(self.ben, self.ann)
        one = self.ann.dm_safety(self.ben._identity.user_id)["number"]
        two = self.ben.dm_safety(self.ann._identity.user_id)["number"]
        self.assertEqual(one, two)
        self.assertEqual(len(one.split()), 12)

    def test_sending_to_a_stranger_or_nothing_is_refused_in_words(self):
        self.assertFalse(self.ann.dm_send("f" * 32, "who?")["ok"])
        self.befriend(self.ann, self.ben)
        self.assertFalse(self.ann.dm_send(self.ben._identity.user_id, "  ")["ok"])
        self.assertFalse(self.ann.dm_send(self.ben._identity.user_id, "", "bm90IGEgcGljdHVyZQ==")["ok"])

    def test_a_picture_arrives_and_can_be_shown(self):
        import base64

        self.befriend(self.ann, self.ben)
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
        self.assertTrue(self.ann.dm_send(self.ben._identity.user_id, "look", base64.b64encode(png).decode())["ok"])
        self.ann._dm.flush()
        (m,) = self.ben.dm_history(self.ann._identity.user_id)["messages"]
        shown = self.ben.dm_image(m["image"])
        self.assertTrue(shown["ok"])
        self.assertTrue(shown["src"].startswith("data:image/png;base64,"))

    def test_the_page_is_not_handed_the_service_or_the_store(self):
        public = {name for name in dir(self.ann) if not name.startswith("_")}
        for hidden in ("dm", "store", "identity", "vault"):
            self.assertNotIn(hidden, public)

    def test_nearby_people_can_be_added_with_the_keys_they_announced(self):
        peer = {"id": "inst", "name": "Cy", "address": "10.0.0.9", "port": None, "sharing": False, "mode": None,
                "user": self.ben._identity.user_id, "sign": self.ben._identity.sign_public.hex(),
                "dh": self.ben._identity.dh_public.hex(), "dm": 5555}
        self.ann._discovery = type("FakeDiscovery", (), {"peer_list": lambda self_: [peer], "stop": lambda self_: None,
                                                         "set_identity": lambda *a: None})()
        self.assertEqual([n["name"] for n in self.ann.dm_state()["nearby"]], ["Cy"])
        self.assertTrue(self.ann.dm_add(self.ben._identity.user_id)["ok"])
        self.assertEqual(self.ann.dm_state()["nearby"], [])
        card = self.ann._store.contact(self.ben._identity.user_id)
        self.assertEqual((card["addr"], card["port"]), ("10.0.0.9", 5555))


class JoinFromOutsideThroughTheControllerTests(unittest.TestCase):
    """One controller hosts a party, another joins it from an invite code, over real HTTPS on this PC."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.host = Controller(data_folder=self.folder + "/host", discovery_port=0, grabber=FakeGrabber())
        self.guest = Controller(data_folder=self.folder + "/guest", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.host.shutdown)
        self.addCleanup(self.guest.shutdown)
        self.opened = []
        self.guest._join = lambda data: self.opened.append(data)  # no window in a test
        self.assertTrue(self.host.start("Host", free_port())["ok"])
        self.guest._name = "Guest"

    def wait_state(self, controller, want, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = controller.join_status()
            if status["state"] == want:
                return status
            time.sleep(0.1)
        return controller.join_status()

    def local_invite(self):
        """The host's invite, rewritten to point at loopback so the test does not depend on the LAN."""
        from privatepartynet import connect

        invite = connect.decode_invite(self.host.invite()["invite"])
        return connect.encode_invite(invite["name"], invite["password"], [["lan", "127.0.0.1", self.host._party.port]],
                                     invite["fingerprint"], invite["server_id"], invite["host_sign"])

    def test_the_host_can_make_an_invite_and_it_carries_the_three_things(self):
        from privatepartynet import connect

        invite = connect.decode_invite(self.host.invite()["invite"])
        self.assertEqual(invite["name"], "Host")
        self.assertEqual(invite["password"], self.host._passcode)
        self.assertTrue(invite["candidates"])
        self.assertEqual(invite["fingerprint"], connect.certificate_fingerprint(self.host._cert_pem))

    def test_an_invite_carries_both_pins_and_a_remembered_party_keeps_the_key_pin(self):
        from privatepartynet import connect
        from privatepartynet.cert import pins_of_pem

        whole, spki = pins_of_pem(self.host._cert_pem)
        invite = connect.decode_invite(self.host.invite()["invite"])
        self.assertEqual((invite["fingerprint"], invite["spki"]), (whole, spki), "f stays what 3.0/3.1 check")
        code = connect.encode_invite(invite["name"], invite["password"], [["lan", "127.0.0.1", self.host._party.port]],
                                     whole, invite["server_id"], invite["host_sign"], spki=spki)
        self.guest.join_invite(code)
        self.wait_state(self.guest, "waiting")
        (request,) = self.host._party.pending_requests()
        self.host._party.decide_request(self.host._party.host_id, request["id"], True)
        self.wait_state(self.guest, "joined")
        record = self.guest._store.server("joined-" + invite["server_id"])
        self.assertEqual((record["fingerprint"], record["spki"]), (whole, spki))

    def test_opening_a_party_window_pins_its_origin_first(self):
        from unittest import mock

        from privatepartynet import windowpins
        from privatepartynet.cert import pins_of_pem

        self.addCleanup(windowpins.forget_all)
        whole, spki = pins_of_pem(self.host._cert_pem)
        url = f"https://127.0.0.1:{self.host._party.port}/join?token=x"
        guest = Controller(data_folder=self.folder + "/window", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(guest.shutdown)
        seen = []

        def create_window(*args, **kwargs):
            seen.append(windowpins.allowed(kwargs["url"], self.host._cert_pem))
            return mock.MagicMock()

        with mock.patch.object(app.webview, "create_window", create_window):
            guest._join({"url": url, "name": "Host", "pins": (whole, spki)})
            guest._join({"url": url.replace("token=x", "token=y"), "name": "Host",
                         "cert": self.host._cert_pem})  # a Nearby accept carries the certificate itself
        self.assertEqual(seen, [True, True])

    def test_there_is_no_invite_when_no_party_is_open(self):
        self.assertFalse(self.guest.invite()["ok"])

    def test_a_joiner_waits_is_let_in_and_the_party_window_opens(self):
        self.assertTrue(self.guest.join_invite(self.local_invite())["ok"])
        self.assertEqual(self.wait_state(self.guest, "waiting")["state"], "waiting")
        (request,) = self.host._party.pending_requests()
        self.assertEqual((request["name"], request["user"]), ("Guest", self.guest._identity.user_id))
        self.host._party.decide_request(self.host._party.host_id, request["id"], True)
        self.assertEqual(self.wait_state(self.guest, "joined")["state"], "joined")
        (opened,) = self.opened
        self.assertIn("/join?token=", opened["url"])
        from privatepartynet.cert import pins_of_pem
        self.assertEqual(set(opened["pins"]), set(pins_of_pem(self.host._cert_pem)),
                         "the window is pinned to the certificate the join checked")
        self.assertEqual(opened["name"], "Host")
        self.assertEqual(self.guest.join_status()["state"], "joined")
        self.assertEqual(len(self.opened), 1, "the window opens once")

    def test_the_party_is_remembered_and_can_be_asked_again(self):
        self.guest.join_invite(self.local_invite())
        self.wait_state(self.guest, "waiting")
        (request,) = self.host._party.pending_requests()
        self.host._party.decide_request(self.host._party.host_id, request["id"], True)
        self.wait_state(self.guest, "joined")
        self.host._party.redeem(self.opened[-1]["url"].split("token=")[1])  # the window would use it; here the test does
        (listed,) = self.guest.joined_servers()
        self.assertEqual(listed["name"], "Host")
        self.assertTrue(self.guest.rejoin(listed["key"])["ok"])
        self.assertEqual(self.wait_state(self.guest, "joined")["state"], "joined",
                         "someone let in before comes back with no one having to say yes")
        self.assertEqual(self.host._party.pending_requests(), [])
        self.assertTrue(self.guest.forget_joined(listed["key"])["ok"])
        self.assertEqual(self.guest.joined_servers(), [])

    def test_the_second_time_the_person_keeps_the_role_they_were_given(self):
        self.guest.join_invite(self.local_invite())
        self.wait_state(self.guest, "waiting")
        (request,) = self.host._party.pending_requests()
        token = self.host._party.decide_request(self.host._party.host_id, request["id"], True)
        session = self.host._party.redeem(token)
        member = self.host._party.member_for(session)
        self.host._party.set_role(self.host._party.host_id, member, "admin")
        token = self.host._party.join_token("Guest", user=self.guest._identity.user_id)
        again = self.host._party.redeem(token)
        self.assertEqual(self.host._party.role_for(again), "admin")

    def test_a_wrong_password_is_reported(self):
        from privatepartynet import connect

        self.host._party.trust_local = False  # the test runs on this PC, which counts as this network
        invite = connect.decode_invite(self.local_invite())
        code = connect.encode_invite(invite["name"], "000000", invite["candidates"], invite["fingerprint"])
        self.guest.join_invite(code)
        status = self.wait_state(self.guest, "failed")
        self.assertIn("password", status["message"])

    def test_a_bad_invite_is_explained(self):
        result = self.guest.join_invite("ppn1-nonsense")
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"])

    def test_joining_by_address_takes_the_certificate_on_trust_and_remembers_it(self):
        port = self.host._party.port
        self.assertTrue(self.guest.join_address("127.0.0.1", port, self.host._passcode, "Host")["ok"])
        self.wait_state(self.guest, "waiting")
        (request,) = self.host._party.pending_requests()
        self.host._party.decide_request(self.host._party.host_id, request["id"], True)
        self.wait_state(self.guest, "joined")
        from privatepartynet import connect

        record = self.guest._store.server_items()[0]["record"]
        self.assertEqual(record["fingerprint"], connect.certificate_fingerprint(self.host._cert_pem))

    def test_nonsense_addresses_are_refused_before_any_connection(self):
        for address in ("", "example.com", "999.1.1.1", "1.2.3"):
            self.assertFalse(self.guest.join_address(address, 8765, "x")["ok"], address)
        self.assertFalse(self.guest.join_address("127.0.0.1", 99999, "x")["ok"])

    def test_only_one_join_at_a_time(self):
        self.guest.join_invite(self.local_invite())
        self.wait_state(self.guest, "waiting")
        self.assertFalse(self.guest.join_invite(self.local_invite())["ok"])
        self.guest.join_cancel()

    def test_a_test_run_does_not_probe_the_network(self):
        self.assertIsNone(self.host.state()["reach"], "no router or STUN calls when the discovery port is 0")

    def test_the_reach_result_is_summarised_for_the_page_once_it_is_in(self):
        self.host._reach = {"state": "done", "reachable": "no", "advice": "shared address", "nat": "easy", "cgnat": True,
                            "ipv6": [], "upnp": {"ok": False}, "candidates": []}
        view = self.host.state()["reach"]
        self.assertEqual((view["state"], view["reachable"], view["cgnat"], view["port_open"]), ("done", "no", True, False))


class ReviewControllerTests(unittest.TestCase):
    """What the review of November 1st found in the controller, fixed and pinned."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.one = Controller(data_folder=self.folder + "/one", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.one.shutdown)

    def test_the_server_password_survives_a_restart_and_the_owner_can_change_it(self):
        port = free_port()
        self.assertTrue(self.one.start("Desk", port)["ok"])
        first = self.one.state()["passcode"]
        self.assertRegex(first, r"^[a-z2-9]{5}-[a-z2-9]{5}$")
        self.one.stop()
        self.one.start("Desk", port)
        self.assertEqual(self.one.state()["passcode"], first)
        self.assertFalse(self.one.set_server_password("abc")["ok"])
        self.assertTrue(self.one.set_server_password("owls at noon")["ok"])
        self.one.stop()
        self.one.start("Desk", port)
        self.assertEqual(self.one.state()["passcode"], "owls at noon")

    def test_the_owner_sets_a_relay_server_that_invites_carry_without_its_secret(self):
        from privatepartynet import connect

        self.assertFalse(self.one.set_relay("relay.example.net", "x" * 20)["ok"], "no party yet")
        self.assertTrue(self.one.start("Desk", free_port())["ok"])
        self.assertIsNone(self.one.state()["relay"])
        self.assertFalse(self.one.set_relay("relay.example.net", "short")["ok"])
        result = self.one.set_relay("relay.example.net:4000", "a-long-enough-relay-secret", "ab" * 32)
        self.assertTrue(result["ok"], result)
        relay = self.one.state()["relay"]
        self.assertEqual((relay["host"], relay["port"], relay["pin"]), ("relay.example.net", 4000, "ab" * 32))
        self.assertNotIn("a-long-enough-relay-secret", repr(self.one.state()))
        code = self.one.invite()["invite"]
        (carried,) = connect.decode_invite(code)["relays"]
        self.assertEqual((carried["kind"], carried["host"], carried["port"]), ("vps", "relay.example.net", 4000))
        self.assertNotIn(b"a-long-enough-relay-secret", repr(connect.decode_invite(code)).encode())
        self.assertTrue(self.one.set_relay("198.51.100.9, [2001:db8::5]:4000", "a-long-enough-relay-secret")["ok"])
        self.assertEqual((self.one.state()["relay"]["host"], self.one.state()["relay"]["port"]),
                         ("198.51.100.9, 2001:db8::5", 4000))
        self.assertTrue(self.one.clear_relay()["ok"])
        self.assertIsNone(self.one.state()["relay"])

    def test_relaying_for_others_is_off_until_turned_on_and_can_be_limited(self):
        self.assertEqual(self.one.state()["friend_relay"], {"on": False, "mbps": 8, "through": [], "problems": [],
                                                            "ways": [], "members": []})
        self.assertFalse(self.one.set_friend_relay(True, "lots")["ok"])
        self.assertTrue(self.one.set_friend_relay(True, 20)["ok"])
        self.assertEqual(self.one.state()["friend_relay"]["on"], True)
        self.assertEqual(self.one.path_settings(), {"friend_relay": True, "friend_relay_mbps": 20})
        again = Controller(data_folder=self.folder + "/one", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(again.shutdown)
        self.assertEqual(again.state()["friend_relay"]["mbps"], 20, "kept in the settings")

    def test_the_panel_shows_who_goes_through_this_pc_and_who_cannot_be_reached(self):
        class Paths:
            names = {"aa" * 16: "Alex", "bb" * 16: "Sam"}
            changed = None

            def state(self):
                return {"through": ["aa" * 16], "peers": {"aa" * 16: {"kind": "ipv6"}, "bb" * 16: {"kind": ""}}}

            def problems(self):
                return [{"peer": "bb" * 16, "why": "Both of your routers give a new port for every connection."}]

            def set_friend_relay(self, on, mbps=None):
                self.changed = (on, mbps)

            def friend_offers(self):
                return [("198.51.100.7", 40001)]

        paths = self.one.path_manager = Paths()
        self.assertEqual(self.one._friend_relays(), [("198.51.100.7", 40001)], "what the party's invites carry")
        view = self.one.state()["friend_relay"]
        self.assertEqual(view["through"], ["Alex"])
        self.assertEqual(view["ways"], ["Alex, direct over IPv6", "Sam, not connected yet"])
        self.assertEqual(view["problems"], ["Sam: Both of your routers give a new port for every connection."])
        self.one.set_friend_relay(True, 4)
        self.assertEqual(paths.changed, (True, 4), "a running game network follows the setting at once")

    def test_updates_wait_for_a_party_and_the_options_follow_the_menu(self):
        self.assertIsNone(self.one.updater._thread, "a test run never asks GitHub")
        self.assertEqual(self.one._update_busy(), "")
        view = self.one.state()["update"]
        self.assertEqual((view["automatic"], view["include_test"]), (True, True))
        self.assertTrue(self.one.start("Desk", free_port())["ok"])
        self.assertEqual(self.one._update_busy(), "your party")
        self.one.stop()
        self.assertEqual(self.one._update_busy(), "")
        self.one._game = {"key": "own"}
        self.assertEqual(self.one._update_busy(), "the game network")
        self.one._game = None
        self.assertTrue(self.one.set_update_option("auto_update", False)["ok"])
        self.assertTrue(self.one.set_update_option("update_prerelease", False)["ok"])
        self.assertFalse(self.one.set_update_option("theme", "dark")["ok"])
        view = self.one.about_info()["update"]
        self.assertEqual((view["automatic"], view["include_test"]), (False, False))

    def test_relay_addresses_are_read_with_or_without_a_port(self):
        from privatepartynet.app import _split_address

        self.assertEqual(_split_address(" relay.example.net "), ("relay.example.net", None))
        self.assertEqual(_split_address("203.0.113.5:4000"), ("203.0.113.5", "4000"))
        self.assertEqual(_split_address("[2001:db8::1]:4000"), ("2001:db8::1", "4000"))
        self.assertEqual(_split_address("2001:db8::1"), ("2001:db8::1", None))

    def test_importing_an_identity_keeps_the_old_one_and_moves_the_listener_to_the_new_keys(self):
        two = Controller(data_folder=self.folder + "/two", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(two.shutdown)
        path = self.folder + "/id.ppnbak"
        two._export_to(path, "pw", True)
        before = self.one._identity.user_id
        old_listener = self.one._dm
        self.one._import_from(path, "pw", True)
        self.assertEqual(self.one._identity.user_id, two._identity.user_id)
        kept = list((Path(self.folder) / "one" / "identity").glob("identity-before-import-*.bin"))
        self.assertEqual(len(kept), 1, "the replaced identity is kept, sealed")
        from privatepartynet.identity import Identity

        self.assertEqual(Identity.from_private_bundle(self.one._vault.unseal("identity", kept[0].read_bytes(), b"identity")).user_id, before)
        self.assertIsNot(self.one._dm, old_listener)
        self.assertEqual(self.one._dm.identity.user_id, two._identity.user_id)

    def test_a_nearby_party_is_asked_over_https_with_the_signed_identity(self):
        host = Controller(data_folder=self.folder + "/host", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(host.shutdown)
        self.assertTrue(host.start("Host", free_port())["ok"])
        peer = {"id": "inst", "name": "Host", "address": "127.0.0.1", "port": host._party.port, "sharing": True,
                "mode": "party", "user": host._identity.user_id, "sign": host._identity.sign_public.hex(),
                "dh": host._identity.dh_public.hex(), "dm": None, "fp": host._party.fingerprint}
        self.one._discovery = type("FakeDiscovery", (), {"peer_list": lambda self_: [peer], "stop": lambda self_: None,
                                                         "set_identity": lambda *a: None, "request": lambda *a: False})()
        opened = []
        self.one._join = lambda data: opened.append(data)
        self.assertTrue(self.one.ask_join("inst")["ok"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not host.state()["requests"]:
            time.sleep(0.1)
        (request,) = host.state()["requests"]
        self.assertTrue(request["id"].startswith("door:"))
        self.assertTrue(host.decide(request["id"], True)["ok"])
        while time.monotonic() < deadline and not opened:
            self.one.join_status()
            time.sleep(0.1)
        self.assertEqual(len(opened), 1)
        members = {m["name"]: m for m in host._party.roster()}
        session = host._party.redeem(opened[0]["url"].split("token=")[1])
        self.assertEqual(host._party._members[host._party.member_for(session)]["user"], self.one._identity.user_id)


class MemberCopyTests(unittest.TestCase):
    """A member's party window hands the chat it receives to the app, which keeps an encrypted copy on this PC."""

    PNG = bytes.fromhex("89504e470d0a1a0a") + bytes(range(50))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.controller = Controller(data_folder=folder.name, discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.controller.shutdown)
        self.api = WindowApi("https://192.168.0.5:8765", store=lambda: self.controller._store)
        self.channels = [{"id": "general", "name": "general", "kind": "text"}, {"id": "voice-1", "name": "Voice 1", "kind": "voice"}]

    def message(self, number, **extra):
        return {"type": "chat", "id": f"{number:012x}", "channel": "general", "seq": number, "from": "m1", "user": "u1",
                "name": "Ann", "text": f"hello {number}", "ts": 1000.0 + number, **extra}

    def test_messages_are_kept_and_read_back_with_the_host_gone(self):
        result = self.api.keep_chat("abcdef0123456789", "Heroes night", self.channels, [self.message(1), self.message(2)])
        self.assertEqual(result, {"ok": True, "missing": []})
        (server,) = self.controller.saved_servers()
        self.assertEqual((server["name"], [c["name"] for c in server["channels"]]), ("Heroes night", ["general"]))
        page = self.controller.saved_history("abcdef0123456789", "general")["messages"]
        self.assertEqual([m["text"] for m in page], ["hello 1", "hello 2"])

    def test_the_same_message_twice_is_kept_once(self):
        self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(1)])
        self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(1)])
        self.assertEqual(self.controller._store.count("srv:abcdef0123456789:general"), 1)

    def test_a_window_cannot_write_into_another_servers_copy(self):
        self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(1)])
        self.assertFalse(self.api.keep_chat("1111111111111111", "Other", self.channels, [self.message(2)])["ok"])
        self.assertEqual(self.controller._store.count("srv:1111111111111111:general"), 0)

    def test_what_a_page_hands_over_is_checked(self):
        bad = [
            "not a dict", {"id": "../x", "channel": "general", "ts": 1.0, "text": "x"},
            self.message(3, ts="soon"), self.message(4, channel=""), self.message(5, text="", image=None),
            self.message(6, text="x" * 2000),
        ]
        self.assertFalse(self.api.keep_chat("not-hex!", "S", self.channels, [self.message(1)])["ok"])
        self.api.keep_chat("abcdef0123456789", "S", self.channels, bad)
        kept = self.controller._store.history("srv:abcdef0123456789:general")
        self.assertEqual([len(m["body"]["text"]) for m in kept], [500], "only the over-long one survives, cut to 500")

    def test_a_missing_picture_is_asked_for_and_kept_only_if_it_is_what_its_name_says(self):
        import base64
        import hashlib

        digest = hashlib.sha256(self.PNG).hexdigest()
        result = self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(1, image={"hash": digest, "w": 10, "h": 10})])
        self.assertEqual(result["missing"], [digest])
        self.assertFalse(self.api.keep_blob(digest, base64.b64encode(bytes.fromhex("89504e470d0a1a0a") + b" other bytes").decode())["ok"])
        self.assertFalse(self.api.keep_blob(hashlib.sha256(b"<svg/>").hexdigest(), base64.b64encode(b"<svg/>").decode())["ok"])
        self.assertTrue(self.api.keep_blob(digest, base64.b64encode(self.PNG).decode())["ok"])
        self.assertEqual(self.controller._store.get_blob(digest), self.PNG)
        again = self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(2, image={"hash": digest, "w": 1, "h": 1})])
        self.assertEqual(again["missing"], [])

    def test_the_hosts_own_window_and_pop_outs_keep_nothing(self):
        self.assertFalse(WindowApi("http://127.0.0.1:1").keep_chat("abcdef0123456789", "S", self.channels, [self.message(1)])["ok"])

    def test_kept_chat_goes_into_a_backup(self):
        self.api.keep_chat("abcdef0123456789", "S", self.channels, [self.message(1)])
        path = tempfile.mkdtemp() + "/b.ppnbak"
        result = self.controller._export_to(path, "pw", False)
        self.assertIn("1 messages", result["message"])


class GameNetworkThroughTheControllerTests(unittest.TestCase):
    """Host and member apps, each with its game network on: the real helper with its adapter in memory (Wintun needs
    administrator rights), and the paths between them in memory."""

    def setUp(self):
        from gamehelper import FakePaths, Hub, helper_program, launch_plain

        program = helper_program()
        if program is None:
            self.skipTest("Go is not installed, so the game network helper cannot be built")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.host = Controller(data_folder=folder.name + "/host", discovery_port=0, grabber=FakeGrabber())
        self.guest = Controller(data_folder=folder.name + "/guest", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.host.shutdown)
        self.addCleanup(self.guest.shutdown)
        self.host_tun, self.guest_tun = queue.Queue(), queue.Queue()
        hub = Hub()
        for controller, arrived in ((self.host, self.host_tun), (self.guest, self.guest_tun)):
            controller._game_launch = launch_plain
            controller._game_helper = {"program": program, "memory_adapter": True, "on_packet": arrived.put}
            controller._game_paths = lambda identity: FakePaths(identity, hub)
        self.assertTrue(self.host.start("Host", free_port())["ok"])
        self.guest._name = "Guest"
        opened = []
        self.guest._join = lambda data: opened.append(data)
        from privatepartynet import connect

        invite = connect.decode_invite(self.host.invite()["invite"])
        code = connect.encode_invite(invite["name"], invite["password"], [["lan", "127.0.0.1", self.host._party.port]],
                                     invite["fingerprint"], invite["server_id"], host_sign=invite["host_sign"])
        self.guest.join_invite(code)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.host._party.pending_requests():
            time.sleep(0.05)
        self.host._party.decide_request(self.host._party.host_id, self.host._party.pending_requests()[0]["id"], True)
        while time.monotonic() < deadline and not opened:
            self.guest.join_status()
            time.sleep(0.05)
        session = self.host._party.redeem(opened[0]["url"].split("token=")[1])
        self.host._party.attach(self.host._party.member_for(session))  # the party window being open

    def got(self, arrived, payload, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if arrived.get(timeout=0.1).endswith(payload):
                    return True
            except queue.Empty:
                continue
        return False

    def wait_on(self, controller, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and controller.game_state()["state"] != "on":
            time.sleep(0.05)
        return controller.game_state()

    def test_a_member_and_the_host_play_over_the_game_network(self):
        from privatepartynet.gamenet import make_packet

        self.assertTrue(self.host.game_use("own")["ok"])
        (joined,) = self.guest.joined_servers()
        self.assertTrue(self.guest.game_use(joined["key"])["ok"])
        host_state, guest_state = self.wait_on(self.host), self.wait_on(self.guest)
        self.assertEqual((host_state["state"], guest_state["state"]), ("on", "on"), (host_state, guest_state))
        self.assertEqual(host_state["network"], guest_state["network"])
        self.assertTrue(host_state["address"].endswith(".1"))
        self.assertEqual(guest_state["active"], joined["key"], "the green light is on this server")
        host_session, guest_session = self.host._game["session"], self.guest._game["session"]
        guest_session.put_packet(make_packet(guest_state["address"], host_state["address"], b"lobby?"))
        self.assertTrue(self.got(self.host_tun, b"lobby?"))
        host_session.put_packet(make_packet(host_state["address"], "255.255.255.255", b"game on port 5000"))
        self.assertTrue(self.got(self.guest_tun, b"game on port 5000"))
        self.assertEqual([m["address"] for m in self.host.game_state()["members"]], [guest_state["address"]])
        self.assertIs(self.host.path_manager, self.host._game["paths"], "the paths are there for the relay controls")
        self.host.game_off()
        self.assertIsNone(self.host.path_manager)

    def test_windows_services_are_closed_to_members_until_this_pc_opens_them_live(self):
        import struct

        from privatepartynet.gamenet import make_packet

        groups = self.host.game_services()["groups"]
        self.assertEqual([g["id"] for g in groups], ["sharing", "remote_desktop", "remote_management", "discovery"])
        self.assertFalse(any(g["open"] for g in groups), "all closed unless opened")
        self.assertTrue(all(g["label"] and g["detail"] for g in groups))
        self.host.game_use("own")
        (joined,) = self.guest.joined_servers()
        self.guest.game_use(joined["key"])
        host_state, guest_state = self.wait_on(self.host), self.wait_on(self.guest)

        def to_host(port, text):
            payload = struct.pack(">HH", 50000, port) + bytes(16) + text
            self.guest._game["session"].put_packet(make_packet(guest_state["address"], host_state["address"], payload, 6))

        to_host(445, b"open the share")
        to_host(27015, b"join my game")
        self.assertTrue(self.got(self.host_tun, b"join my game"))
        self.assertFalse(self.got(self.host_tun, b"open the share", seconds=0.5))
        self.assertTrue(self.host.game_set_service("sharing", True)["groups"][0]["open"])
        time.sleep(0.3)  # the helper takes the change from the app's link
        to_host(445, b"open the share")
        self.assertTrue(self.got(self.host_tun, b"open the share"), "opened at once, with no new permission prompt")
        self.assertEqual(load_settings(self.host._data_dir)["game_open"], ["sharing"], "kept for next time")
        self.host.game_set_service("sharing", False)
        time.sleep(0.3)
        to_host(445, b"open the share again")
        to_host(27015, b"marker")
        self.assertTrue(self.got(self.host_tun, b"marker"))
        self.assertFalse(self.got(self.host_tun, b"open the share again", seconds=0.5))
        self.assertFalse(self.host.game_set_service("everything", True)["ok"])

    def test_a_removed_member_is_dropped_by_the_host_at_once(self):
        self.host.game_use("own")
        (joined,) = self.guest.joined_servers()
        self.guest.game_use(joined["key"])
        self.wait_on(self.host), self.wait_on(self.guest)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.host.game_state()["members"]:
            time.sleep(0.05)
        party = self.host._party
        guest_id = next(m["id"] for m in party.roster() if m["role"] == "member")
        party.kick(party.host_id, guest_id)
        while time.monotonic() < deadline and self.host.game_state()["members"]:
            time.sleep(0.05)
        self.assertEqual(self.host.game_state()["members"], [])
        while time.monotonic() < deadline and self.guest.game_state()["active"]:
            time.sleep(0.05)
        self.assertEqual(self.guest.game_state()["active"], "", "the removed member's game network turns off")

    def test_the_choice_is_remembered_and_turning_it_off_clears_the_light(self):
        self.host.game_use("own")
        self.assertEqual(self.wait_on(self.host)["state"], "on")
        self.assertEqual(load_settings(self.host._data_dir)["game_server"], "own")
        self.host.game_off()
        state = self.host.game_state()
        self.assertEqual((state["active"], state["chosen"]), ("", "own"))

    def test_stopping_the_party_turns_your_own_game_network_off(self):
        self.host.game_use("own")
        self.wait_on(self.host)
        self.host.stop()
        self.assertEqual(self.host.game_state()["active"], "")

    def test_an_unknown_server_or_no_party_is_said_plainly(self):
        self.assertFalse(self.guest.game_use("own")["ok"])
        self.assertFalse(self.guest.game_use("joined-nothing")["ok"])
