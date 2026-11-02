"""The app's side of the game network adapter: start the elevated helper and pass packets between it and the network.

GameSession listens on a loopback port, writes a one-time secret to a file only this user can read, and starts the helper
with Windows' permission prompt (ShellExecute "runas"). The helper connects back with the secret; from then on packets
from the adapter go to `uplink(packet)` (the member's link to the host, or the host's own router) and packets from the
network are handed to the adapter with deliver(packet). Closing the session closes the socket, which ends the helper.
"""

import ctypes
import hmac
import os
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path

from privatepartynet import gamenet

ACCEPT_SECONDS = 180  # how long the permission prompt may stay open before the attempt is given up
SW_HIDE = 0


def helper_command(port, secret_file, address, network, parent):
    """(program, arguments, folder) to start the helper: the packaged app itself, or pythonw running the module."""
    arguments = ["--net-helper", "--port", str(port), "--secret-file", str(secret_file), "--address", str(address),
                 "--network", str(network), "--parent", str(parent)]
    if getattr(sys, "frozen", False):
        return sys.executable, arguments, str(Path(sys.executable).parent)
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    program = str(windowless if windowless.exists() else python)
    return program, ["-m", "privatepartynet.nethelper"] + arguments, str(Path(__file__).resolve().parent.parent)


def quote(argument):
    text = str(argument)
    return '"' + text.replace('"', '\\"') + '"' if (" " in text or '"' in text or not text) else text


def launch_elevated(program, arguments, folder):
    """Start a program with administrator rights. Windows shows its permission prompt. Returns None when it started, or
    the reason it did not ("declined" when the person said no)."""
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, " ".join(quote(a) for a in arguments), folder,
                                                 SW_HIDE)
    if result > 32:
        return None
    return "declined" if result == 5 else f"could not start ({result})"


class GameSession:
    def __init__(self, address, network, uplink, folder, launch=None, accept_seconds=ACCEPT_SECONDS):
        self.address, self.network = str(address), str(network)
        self._uplink = uplink
        self._folder = Path(folder)
        self._launch = launch or (lambda program, arguments, cwd: launch_elevated(program, arguments, cwd))
        self._accept_seconds = accept_seconds
        self._stop = threading.Event()
        self._sock = None
        self._send_lock = threading.Lock()
        self._thread = None
        self.status = {"state": "idle", "message": ""}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="privatepartynet-game", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def deliver(self, packet):
        """Hand one packet from the network to the adapter. Dropped while the helper is not connected."""
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendall(gamenet.FRAME_HEADER.pack(len(packet)) + packet)
            return True
        except OSError:
            return False

    def _set(self, state, message=""):
        self.status = {"state": state, "message": message}

    def _run(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1.0)
        port = listener.getsockname()[1]
        secret = secrets.token_hex(32)
        self._folder.mkdir(parents=True, exist_ok=True)
        secret_file = self._folder / f"net-{secrets.token_hex(6)}.key"
        secret_file.write_text(secret, encoding="ascii")
        try:
            self._set("asking", "Windows is asking for permission to add the game network adapter.")
            program, arguments, cwd = helper_command(port, secret_file, self.address, self.network, os.getpid())
            problem = self._launch(program, arguments, cwd)
            if problem:
                self._set("declined" if problem == "declined" else "failed",
                          "Permission was not given, so the game network is off." if problem == "declined"
                          else f"The game network helper {problem}.")
                return
            conn = self._accept(listener, secret)
            if conn is None:
                return
            self._sock = conn
            self._set("on", f"Game network on: this PC is {self.address}.")
            self._pump(conn)
        finally:
            listener.close()
            try:
                secret_file.unlink()
            except OSError:
                pass
            if self.status["state"] == "on":
                self._set("off", "The game network is off.")

    def _accept(self, listener, secret):
        waited = 0.0
        while not self._stop.is_set() and waited < self._accept_seconds:
            try:
                conn, peer = listener.accept()
            except socket.timeout:
                waited += 1.0
                continue
            conn.settimeout(10.0)
            try:
                line = b""
                while not line.endswith(b"\n") and len(line) < 100:
                    chunk = conn.recv(1)
                    if not chunk:
                        break
                    line += chunk
            except OSError:
                line = b""
            if peer[0] == "127.0.0.1" and hmac.compare_digest(line.strip(), secret.encode("ascii")):
                conn.settimeout(None)
                return conn
            conn.close()  # not our helper: keep waiting for the real one
        if not self._stop.is_set():
            self._set("failed", "The game network helper did not start in time.")
        return None

    def _pump(self, conn):
        class Stream:
            def read(self, count):
                return conn.recv(count)

        stream = Stream()
        network = __import__("ipaddress").ip_network(self.network)
        try:
            while not self._stop.is_set():
                packet = gamenet.read_frame(stream)
                if packet and gamenet.carried(packet, network):
                    self._uplink(packet)
        except (EOFError, OSError, ValueError):
            pass
