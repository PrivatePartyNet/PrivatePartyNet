"""The app and the helper together, with an in-memory adapter in place of Wintun (which needs administrator rights).

The launch the app would do with Windows' permission prompt is replaced by starting the helper's own serve() in a
thread, connected exactly as the real helper connects: to the loopback port, with the secret read from the file."""

import ipaddress
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from privatepartynet import nethelper
from privatepartynet.gameclient import GameSession, helper_command
from privatepartynet.gamenet import MemoryTun, make_packet, parse

NET = "10.77.12.0/24"


class FakeLaunch:
    """Starts the helper's serve() in a thread instead of an elevated process, reading the same arguments."""

    def __init__(self, tun, secret_override=None, answer=None, parent_alive=lambda pid: True):
        self.tun = tun
        self.secret_override = secret_override
        self.answer = answer
        self.alive = parent_alive
        self.why = queue.Queue()
        self.arguments = None

    def __call__(self, program, arguments, cwd):
        self.arguments = arguments
        if self.answer:
            return self.answer
        values = dict(zip(arguments[::2], arguments[1::2])) if arguments[0] != "-m" else dict(zip(arguments[3::2], arguments[4::2]))
        port = int(values["--port"])
        secret = self.secret_override or Path(values["--secret-file"]).read_text(encoding="ascii").strip()

        def run():
            sock = nethelper.connect_to_app(port, secret)
            self.why.put(nethelper.serve(self.tun, sock, values["--network"], parent=int(values["--parent"]), alive=self.alive))

        threading.Thread(target=run, daemon=True).start()
        return None


class GameSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.sent = queue.Queue()

    def session(self, launch, accept_seconds=5):
        session = GameSession("10.77.12.2", NET, self.sent.put, self.folder, launch=launch, accept_seconds=accept_seconds)
        session.start()
        self.addCleanup(session.stop)
        return session

    def wait(self, session, state, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and session.status["state"] != state:
            time.sleep(0.05)
        return session.status["state"] == state

    def test_packets_go_both_ways_and_only_game_network_traffic_passes(self):
        tun = MemoryTun()
        session = self.session(FakeLaunch(tun))
        self.assertTrue(self.wait(session, "on"))
        self.assertIn("10.77.12.2", session.status["message"])
        tun.outgoing.put(make_packet("10.77.12.2", "8.8.8.8", b"to the internet"))
        tun.outgoing.put(make_packet("10.77.12.2", "10.77.12.3", b"to sam"))
        self.assertEqual(self.sent.get(timeout=3)[-6:], b"to sam", "the internet packet never left the adapter")
        self.assertTrue(session.deliver(make_packet("10.77.12.3", "10.77.12.2", b"from sam")))
        self.assertEqual(tun.incoming.get(timeout=3)[-8:], b"from sam")
        session.deliver(make_packet("10.77.12.3", "192.168.0.10", b"into the home network"))
        with self.assertRaises(queue.Empty):
            tun.incoming.get(timeout=0.5)

    def test_the_secret_file_is_gone_once_used_and_never_on_the_command_line(self):
        launch = FakeLaunch(MemoryTun())
        session = self.session(launch)
        self.assertTrue(self.wait(session, "on"))
        self.assertNotIn("secret", " ".join(a for a in launch.arguments if not a.endswith(".key")).replace("--secret-file", ""))
        session.stop()
        time.sleep(0.3)
        self.assertEqual(list(self.folder.glob("net-*.key")), [])

    def test_a_process_without_the_secret_is_turned_away(self):
        tun = MemoryTun()
        launch = FakeLaunch(tun, secret_override="0" * 64)
        session = self.session(launch, accept_seconds=2)
        self.assertTrue(self.wait(session, "failed", seconds=6))
        self.assertIn("did not start in time", session.status["message"])

    def test_saying_no_to_the_permission_prompt_leaves_it_off(self):
        session = self.session(FakeLaunch(MemoryTun(), answer="declined"))
        self.assertTrue(self.wait(session, "declined"))
        self.assertIn("Permission was not given", session.status["message"])

    def test_turning_it_off_ends_the_helper(self):
        launch = FakeLaunch(MemoryTun())
        session = self.session(launch)
        self.assertTrue(self.wait(session, "on"))
        session.stop()
        self.assertEqual(launch.why.get(timeout=5), "the app closed the connection")

    def test_the_helper_quits_when_the_app_is_gone(self):
        alive = {"yes": True}
        launch = FakeLaunch(MemoryTun(), parent_alive=lambda pid: alive["yes"])
        session = self.session(launch)
        self.assertTrue(self.wait(session, "on"))
        alive["yes"] = False
        self.assertEqual(launch.why.get(timeout=6), "the app is gone")

    def test_the_helper_has_a_time_limit(self):
        tun = MemoryTun()
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        started = time.monotonic()
        why = nethelper.serve(tun, b, NET, max_seconds=0.5)
        self.assertEqual(why, "the time limit was reached")
        self.assertLess(time.monotonic() - started, 5)


class HelperPieceTests(unittest.TestCase):
    def test_the_adapter_gets_an_address_and_a_low_metric_and_no_gateway(self):
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        nethelper.configure("PrivatePartyNet", "10.77.12.2", NET, run=lambda command, **kw: calls.append((command, kw)) or Done())
        first, second = calls[0][0], calls[1][0]
        self.assertIn("address=10.77.12.2", first)
        self.assertIn("mask=255.255.255.0", first)
        self.assertIn("gateway=none", first)
        self.assertIn("metric=1", second)
        self.assertTrue(all(kw.get("creationflags") == nethelper.NO_WINDOW for _, kw in calls), "no console windows")
        self.assertTrue(all(kw.get("timeout") for _, kw in calls), "every command is bounded")

    def test_the_network_is_made_private_and_retried_until_windows_has_made_it(self):
        calls = []

        class Result:
            def __init__(self, code):
                self.returncode, self.stdout, self.stderr = code, "", ""

        answers = iter([1, 1, 0])
        ok = nethelper.make_private("PrivatePartyNet", run=lambda command, **kw: calls.append((command, kw)) or Result(next(answers)),
                                    sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertIn("-NetworkCategory Private", calls[0][0][-1])
        self.assertTrue(all(kw.get("creationflags") == nethelper.NO_WINDOW and kw.get("timeout") for _, kw in calls))
        never = nethelper.make_private("PrivatePartyNet", run=lambda command, **kw: Result(1), tries=3, sleep=lambda s: None)
        self.assertFalse(never, "gives up after its tries, without raising")

    def fake_powershell(self, interfaces):
        """A stand-in for PowerShell that keeps adapters' metrics and answers the helper's three kinds of command."""
        import json as json_module
        import re as re_module

        state = {i["InterfaceIndex"]: dict(i) for i in interfaces}
        calls = []

        class Result:
            def __init__(self, out=""):
                self.returncode, self.stdout, self.stderr = 0, out, ""

        def run(command, **kw):
            script = command[-1]
            calls.append(script)
            if script.startswith("Get-NetIPInterface"):
                low = [i for i in state.values() if i["InterfaceMetric"] <= 1 and i["InterfaceAlias"] != "PrivatePartyNet"]
                return Result(json_module.dumps(low[0] if len(low) == 1 else low) if low else "")
            index = int(re_module.search(r"-InterfaceIndex (\d+)", script).group(1))
            if "-AutomaticMetric Enabled" in script:
                state[index]["Automatic"], state[index]["InterfaceMetric"] = "Enabled", 25
            else:
                state[index]["InterfaceMetric"] = int(re_module.search(r"-InterfaceMetric (\d+)", script).group(1))
            return Result()

        return state, calls, run

    def test_radmin_steps_aside_while_on_and_gets_exactly_its_setting_back(self):
        state, calls, run = self.fake_powershell([
            {"InterfaceIndex": 7, "InterfaceAlias": "Radmin VPN", "InterfaceMetric": 1, "Automatic": "Disabled"},
            {"InterfaceIndex": 9, "InterfaceAlias": "Wi-Fi", "InterfaceMetric": 35, "Automatic": "Enabled"}])
        state_file = Path(tempfile.mkdtemp()) / "net-metrics.json"
        moved = nethelper.take_priority("PrivatePartyNet", state_file, run=run)
        self.assertEqual([m["alias"] for m in moved], ["Radmin VPN"])
        self.assertEqual(state[7]["InterfaceMetric"], 2)
        self.assertEqual(state[9]["InterfaceMetric"], 35, "adapters above 1 are not touched")
        self.assertTrue(state_file.exists(), "what to give back is on disk before anything changes")
        nethelper.give_back(moved, state_file, run=run)
        self.assertEqual(state[7]["InterfaceMetric"], 1)
        self.assertEqual(state[7]["Automatic"], "Disabled")
        self.assertFalse(state_file.exists())

    def test_an_automatic_metric_is_given_back_as_automatic(self):
        state, calls, run = self.fake_powershell([
            {"InterfaceIndex": 4, "InterfaceAlias": "Other VPN", "InterfaceMetric": 1, "Automatic": "Enabled"}])
        state_file = Path(tempfile.mkdtemp()) / "net-metrics.json"
        moved = nethelper.take_priority("PrivatePartyNet", state_file, run=run)
        nethelper.give_back(moved, state_file, run=run)
        self.assertIn("-AutomaticMetric Enabled", calls[-1])

    def test_after_a_crash_the_next_start_gives_the_metrics_back_first(self):
        state, calls, run = self.fake_powershell([
            {"InterfaceIndex": 7, "InterfaceAlias": "Radmin VPN", "InterfaceMetric": 1, "Automatic": "Disabled"}])
        state_file = Path(tempfile.mkdtemp()) / "net-metrics.json"
        nethelper.take_priority("PrivatePartyNet", state_file, run=run)  # ...and then the helper is killed: no give_back
        self.assertEqual(state[7]["InterfaceMetric"], 2)
        restored = nethelper.restore_leftovers(state_file, run=run)
        self.assertEqual([r["alias"] for r in restored], ["Radmin VPN"])
        self.assertEqual(state[7]["InterfaceMetric"], 1)
        self.assertFalse(state_file.exists())

    def test_nothing_at_metric_one_means_nothing_changes(self):
        state, calls, run = self.fake_powershell([
            {"InterfaceIndex": 9, "InterfaceAlias": "Wi-Fi", "InterfaceMetric": 35, "Automatic": "Enabled"}])
        state_file = Path(tempfile.mkdtemp()) / "net-metrics.json"
        self.assertEqual(nethelper.take_priority("PrivatePartyNet", state_file, run=run), [])
        self.assertFalse(state_file.exists())
        self.assertEqual(len(calls), 1, "only the look")

    def test_a_failing_netsh_is_reported(self):
        class Failed:
            returncode, stdout, stderr = 1, "The filename, directory name, or volume label syntax is incorrect.", ""

        with self.assertRaises(OSError):
            nethelper.configure("PrivatePartyNet", "10.77.12.2", NET, run=lambda command, **kw: Failed())

    def test_the_helper_is_started_windowless_from_source(self):
        program, arguments, folder = helper_command(1234, "C:/x/net.key", "10.77.12.2", NET, 99)
        self.assertTrue(program.lower().endswith("pythonw.exe") or program.lower().endswith("python.exe"))
        self.assertEqual(arguments[:2], ["-m", "privatepartynet.nethelper"])
        self.assertIn("--parent", arguments)

    def test_a_dead_process_is_seen_as_gone(self):
        import subprocess
        process = subprocess.Popen(["cmd", "/c", "exit 0"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process.wait(timeout=10)
        self.assertFalse(nethelper.parent_alive(process.pid))
        import os
        self.assertTrue(nethelper.parent_alive(os.getpid()))

    def test_the_bundled_wintun_is_the_signed_original(self):
        path = nethelper.wintun_path()
        if not path.exists():
            self.skipTest("wintun.dll is not in assets yet")
        import hashlib
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), WINTUN_AMD64_SHA256)


WINTUN_AMD64_SHA256 = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"  # bin/amd64 of wintun-0.14.1.zip, whose hash matched wintun.net


if __name__ == "__main__":
    unittest.main()
