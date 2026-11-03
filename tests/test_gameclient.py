"""The app and the real game network helper together, with the helper's adapter in memory (Wintun needs administrator
rights) and the paths between members in memory.

The launch the app would do with Windows' permission prompt is replaced by starting the built helper as a normal
process, connected exactly as the real one connects: to the loopback port, with the secret read from the file. These
tests build the helper from wghelper/ and are skipped when Go is not installed."""

import ipaddress
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gamehelper import FakePaths, Hub, helper_program, launch_plain
from privatepartynet import gameclient
from privatepartynet.gameclient import GameMesh, GameSession, helper_command, member_byte
from privatepartynet.gamenet import make_packet, new_private_key, public_key

NET = "10.77.12.0/24"


class Member:
    """One PC on the game network: its session with the helper, its paths, and what reached its "Windows"."""

    def __init__(self, test, user, address, hub, kind="lan"):
        self.user, self.address = user, address
        self.private = new_private_key()
        self.key = public_key(self.private)
        self.arrived = queue.Queue()
        self.paths = FakePaths(user, hub, kind=kind)
        self.session = GameSession(address, NET, self.private, test.folder, launch=launch_plain, program=test.program,
                                   memory_adapter=True, on_packet=self.arrived.put)
        self.mesh = GameMesh(user, self.paths, self.session)
        self.session.start()
        test.addCleanup(self.session.stop)
        test.addCleanup(self.paths.stop)

    def entry(self):
        return {"user": self.user, "address": self.address, "key": self.key, "candidates": []}

    def send(self, destination, payload):
        self.session.put_packet(make_packet(self.address, destination, payload))

    def got(self, payload, seconds=8):
        """True when a packet ending in `payload` reached this PC's "Windows" within `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if self.arrived.get(timeout=0.1).endswith(payload):
                    return True
            except queue.Empty:
                continue
        return False


class GameNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.program = helper_program()
        if cls.program is None:
            raise unittest.SkipTest("Go is not installed, so the game network helper cannot be built")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.hub = Hub()

    def wait_on(self, *members, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and any(m.session.status["state"] != "on" for m in members):
            time.sleep(0.05)
        return [m.session.status["state"] for m in members]

    def mesh(self, count, kind="lan"):
        members = [Member(self, f"u{n}", f"10.77.12.{n}", self.hub, kind) for n in range(1, count + 1)]
        self.assertEqual(self.wait_on(*members), ["on"] * count)
        self.roster = {"peers": [m.entry() for m in members]}
        for m in members:
            m.mesh.apply(self.roster)
        return members

    def test_members_reach_each_other_one_to_one_and_all_at_once(self):
        host, alex, sam = self.mesh(3)
        alex.send(sam.address, b"your move")
        self.assertTrue(sam.got(b"your move"))
        self.assertFalse(host.got(b"your move", seconds=0.5), "a packet for one member goes to that member only")
        sam.send("10.77.12.255", b"anyone hosting?")
        self.assertTrue(alex.got(b"anyone hosting?") and host.got(b"anyone hosting?"))
        host.send("239.255.255.250", b"multicast lobby")
        self.assertTrue(alex.got(b"multicast lobby") and sam.got(b"multicast lobby"))

    def test_only_game_network_traffic_leaves_and_nothing_else_comes_in(self):
        host, alex = self.mesh(2)
        alex.send("8.8.8.8", b"to the internet")
        alex.send("192.168.1.20", b"into a home network")
        alex.session.put_packet(make_packet("10.77.12.9", host.address, b"from another address"))
        alex.send(host.address, b"marker")
        self.assertTrue(host.got(b"marker"))
        self.assertFalse(host.got(b"internet", seconds=0.3) or host.got(b"home network", seconds=0.1)
                         or host.got(b"another address", seconds=0.1))

    def test_someone_removed_from_the_list_is_cut_off_at_once(self):
        host, alex, sam = self.mesh(3)
        sam.send(host.address, b"before")
        self.assertTrue(host.got(b"before"))
        without_sam = {"peers": [host.entry(), alex.entry()]}
        host.mesh.apply(without_sam)
        alex.mesh.apply(without_sam)
        self.assertNotIn("u3", host.paths.paths, "the path to them is closed")
        sam.send(host.address, b"after")
        sam.send("10.77.12.255", b"after, to everyone")
        self.assertFalse(host.got(b"after", seconds=1) or alex.got(b"to everyone", seconds=0.3))
        alex.send(host.address, b"still here")
        self.assertTrue(host.got(b"still here"))

    def test_a_path_that_starts_working_later_starts_the_handshake_at_once(self):
        host, alex = self.mesh(2, kind="")  # no way to each other yet: what WireGuard sends is dropped
        alex.send(host.address, b"too early")
        self.assertFalse(host.got(b"too early", seconds=0.5))
        started = time.monotonic()
        for member in (host, alex):
            member.paths.switch("punch")
        alex.send(host.address, b"now")
        self.assertTrue(host.got(b"now", seconds=4))
        self.assertLess(time.monotonic() - started, 4, "not after WireGuard's own five-second retry")
        self.assertEqual([m["kind"] for m in alex.mesh.members()], ["punch"])

    def test_datagrams_fit_every_path_even_for_a_full_size_broadcast(self):
        host, alex = self.mesh(2)
        big = b"x" * (1320 - 20)  # the adapter's largest packet
        alex.send("10.77.12.255", big)
        self.assertTrue(host.got(big))
        self.assertEqual((host.paths.oversized, alex.paths.oversized), (0, 0))

    def test_the_secret_file_is_gone_once_used_and_never_on_the_command_line(self):
        seen = {}

        def launch(program, arguments, cwd):
            seen["arguments"] = arguments
            return launch_plain(program, arguments, cwd)

        session = GameSession("10.77.12.2", NET, new_private_key(), self.folder, launch=launch, program=self.program,
                              memory_adapter=True)
        session.start()
        self.addCleanup(session.stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and session.status["state"] != "on":
            time.sleep(0.05)
        self.assertEqual(session.status["state"], "on")
        secret_file = Path(seen["arguments"][seen["arguments"].index("--secret-file") + 1])
        self.assertFalse(secret_file.exists())
        self.assertFalse(any(len(a) == 64 and all(c in "0123456789abcdef" for c in a) for a in seen["arguments"]))

    def test_no_other_program_can_share_the_helpers_port(self):
        def squatter(program, arguments, cwd):
            port = int(arguments[arguments.index("--port") + 1])
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                seen["bound"] = True
            except OSError:
                seen["bound"] = False
            finally:
                sock.close()
            return "declined"

        seen = {}
        session = GameSession("10.77.12.2", NET, new_private_key(), self.folder, launch=squatter, program=self.program)
        session.start()
        session._thread.join(timeout=5)
        self.assertEqual(seen, {"bound": False})

    def test_a_process_without_the_secret_is_turned_away(self):
        def impostor(program, arguments, cwd):
            port = int(arguments[arguments.index("--port") + 1])
            sock = socket.create_connection(("127.0.0.1", port))
            sock.sendall(b"0" * 64 + b"\n")
            threading.Timer(2.0, sock.close).start()
            return None

        session = GameSession("10.77.12.2", NET, new_private_key(), self.folder, launch=impostor, program=self.program,
                              accept_seconds=2)
        session.start()
        self.addCleanup(session.stop)
        time.sleep(3.5)
        self.assertEqual(session.status["state"], "failed")

    def test_settings_outside_the_game_network_are_refused_by_the_helper(self):
        session = GameSession("192.168.1.5", "192.168.1.0/24", new_private_key(), self.folder, launch=launch_plain,
                              program=self.program, memory_adapter=True)
        session.start()
        self.addCleanup(session.stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and session.status["state"] != "failed":
            time.sleep(0.05)
        self.assertEqual(session.status["state"], "failed")
        self.assertIn("not a game network", session.status["message"])

    def test_turning_it_off_ends_the_helper(self):
        started = []

        def launch(program, arguments, cwd):
            started.append(subprocess.Popen([program, *arguments], cwd=cwd,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            return None

        session = GameSession("10.77.12.2", NET, new_private_key(), self.folder, launch=launch, program=self.program,
                              memory_adapter=True)
        session.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and session.status["state"] != "on":
            time.sleep(0.05)
        self.assertIsNone(started[0].poll(), "running while on")
        session.stop()
        self.assertEqual(started[0].wait(timeout=10), 0, "it ends, on its own, when the app's link closes")


class SessionPieceTests(unittest.TestCase):
    def test_what_windows_would_not_do_is_said_in_plain_words(self):
        import json

        session = GameSession("10.77.12.2", NET, new_private_key(), ".")
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        warning = "Windows kept this network Public, so games hosted on this PC may be blocked by the firewall."
        b.sendall(gameclient.frame("S", json.dumps({"state": "on", "warnings": []}).encode()))
        b.sendall(gameclient.frame("S", json.dumps({"state": "on", "warnings": [warning, 7]}).encode()))
        b.close()
        session._pump(a)
        self.assertEqual(session.status["state"], "on")
        self.assertEqual(session.status["message"], f"Game network on: this PC is 10.77.12.2. {warning}")
        self.assertEqual(session.status["warnings"], [warning])

    def test_saying_no_to_the_permission_prompt_leaves_it_off(self):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / gameclient.HELPER
            program.write_bytes(b"")
            session = GameSession("10.77.12.2", NET, new_private_key(), folder, launch=lambda *a: "declined",
                                  program=program)
            session.start()
            session._thread.join(timeout=5)
            self.assertEqual(session.status["state"], "declined")

    def test_a_missing_helper_is_said_plainly(self):
        with tempfile.TemporaryDirectory() as folder:
            session = GameSession("10.77.12.2", NET, new_private_key(), folder, program=Path(folder) / "none.exe",
                                  launch=lambda *a: self.fail("nothing to start"))
            session.start()
            session._thread.join(timeout=5)
            self.assertEqual(session.status["state"], "failed")
            self.assertIn("missing", session.status["message"])

    def test_the_helper_is_started_from_next_to_wintun(self):
        program, arguments, folder = helper_command(1234, "C:/x/net.key", 99)
        self.assertTrue(program.endswith(gameclient.HELPER))
        self.assertEqual(Path(folder), Path(program).parent)
        self.assertTrue((Path(folder) / "wintun.dll").exists(), "wintun.dll is loaded from the helper's own folder")
        self.assertEqual(arguments, ["--port", "1234", "--secret-file", "C:/x/net.key", "--parent", "99"])

    def test_datagrams_for_someone_removed_or_unknown_are_dropped_quietly(self):
        class Session:
            address = "10.77.12.1"

            def __init__(self):
                self.delivered, self.peers = [], None

            def deliver(self, member, datagram):
                self.delivered.append(member)

            def path_up(self, member):
                pass

            def set_peers(self, peers):
                self.peers = peers

        session, paths = Session(), FakePaths("u1", Hub())
        mesh = GameMesh("u1", paths, session)
        sam = {"user": "u3", "address": "10.77.12.3", "key": public_key(new_private_key()), "candidates": []}
        mesh.apply({"peers": [sam]})
        old_path = paths.paths["u3"]
        mesh.apply({"peers": []})  # Sam was removed
        self.assertEqual(session.peers, [])
        self.assertTrue(old_path.closed)
        mesh.on_datagram(3, b"\x04" + bytes(40))  # WireGuard still had something queued for Sam
        mesh.on_datagram(200, b"\x01" + bytes(147))  # nobody at all
        old_path._deliver(b"\x04" + bytes(40))  # a late datagram on Sam's closed path
        self.assertEqual(session.delivered, [])

    def test_a_member_is_named_by_the_last_byte_of_their_address(self):
        self.assertEqual(member_byte("10.77.12.7"), 7)
        self.assertEqual(member_byte(ipaddress.ip_address("10.77.12.254")), 254)

    def test_the_bundled_wintun_is_the_signed_original(self):
        from privatepartynet.paths import asset_path

        path = asset_path("wintun.dll")
        if not path.exists():
            self.skipTest("wintun.dll is not in assets yet")
        import hashlib
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), WINTUN_AMD64_SHA256)


class HelperTests(unittest.TestCase):
    def test_the_helpers_own_go_tests_pass(self):
        import gohelper

        if gohelper.find_go() is None:
            self.skipTest("Go is not installed")
        gohelper.test()


WINTUN_AMD64_SHA256 = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"  # bin/amd64 of wintun-0.14.1.zip, whose hash matched wintun.net


if __name__ == "__main__":
    unittest.main()
