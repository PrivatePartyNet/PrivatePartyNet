"""The app's side of the game network: start the elevated helper, and carry WireGuard's datagrams between the helper and
the paths to each member.

GameSession listens on a loopback port, writes a one-time secret to a file only this user can read, and starts the helper
(wghelper/, built as PrivatePartyNetGameNet.exe next to wintun.dll) with Windows' permission prompt (ShellExecute
"runas"). The helper connects back with the secret, and the session sends it this PC's WireGuard key, address and the
members (the helper checks all of it: it runs with administrator rights). From then on each WireGuard datagram for a
member comes out of the helper tagged with that member, and goes along that member's path; datagrams from a member's
path go back in. Closing the session closes the socket, which ends the helper.

Frames on the loopback link: two bytes of length (big-endian, counting what follows), one byte of kind, then the body
(see wghelper/link.go). A member is named by the last byte of their address, which is unique on a /24.

GameMesh follows the host's list of who is on: it opens a path to each member through the path manager (pathway.py),
tells the helper who the members are, and closes the path of anyone who leaves.
"""

import ctypes
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import secrets
import socket
import struct
import threading
from pathlib import Path

from privatepartynet.paths import asset_path

log = logging.getLogger("privatepartynet.game")

ACCEPT_SECONDS = 180  # how long the permission prompt may stay open before the attempt is given up
SW_HIDE = 0
HELPER = "PrivatePartyNetGameNet.exe"
FRAME = struct.Struct(">HB")
OUTBOX = 2048  # frames waiting for the helper; more are dropped (WireGuard copes with lost datagrams)
GAME_MTU = 1270  # the adapter's largest packet (wghelper/main.go MTU, with its arithmetic)
HANDSHAKE_START = b"\x01"  # the first byte of WireGuard's handshake initiation


def helper_path():
    """The helper program, next to wintun.dll so it loads that copy."""
    return asset_path(HELPER)


def helper_command(port, secret_file, parent, program=None, memory_adapter=False):
    """(program, arguments, folder) to start the helper."""
    program = Path(program or helper_path())
    arguments = ["--port", str(port), "--secret-file", str(secret_file), "--parent", str(parent)]
    if memory_adapter:
        arguments.append("--memory-adapter")
    return str(program), arguments, str(program.parent)


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


def app_proof(secret):
    """What the app answers to the helper's secret, so the helper knows it reached the app that wrote it and not another
    program on the port (wghelper/main.go AppProof)."""
    return hmac.new(secret.encode("ascii"), b"PrivatePartyNet app", hashlib.sha256).hexdigest().encode("ascii")


def frame(kind, *parts):
    body = b"".join(parts)
    return FRAME.pack(1 + len(body), ord(kind)) + body


def read_frame(sock):
    """(kind, body) of the next frame. Raises EOFError when the link closes."""
    length, kind = FRAME.unpack(_read_exact(sock, FRAME.size))
    return chr(kind), _read_exact(sock, length - 1) if length > 1 else b""


def _read_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError("the link closed")
        data += chunk
    return data


def member_byte(address):
    return ipaddress.IPv4Address(address).packed[3]


class GameSession:
    """The elevated helper and its link. `on_datagram(member byte, datagram)` gets each datagram WireGuard sends; GameMesh
    sets it."""

    def __init__(self, address, network, private_key, folder, on_datagram=None, launch=None,
                 accept_seconds=ACCEPT_SECONDS, program=None, memory_adapter=False, on_packet=None, open_services=()):
        self.address, self.network = str(address), str(network)
        self._private_key = private_key
        self._peers = []
        self._open = sorted(open_services)  # Windows services members may reach on this PC (gamenet.SERVICE_GROUPS)
        self.on_datagram = on_datagram or (lambda member, datagram: None)
        self._on_packet = on_packet  # tests: packets for "Windows" with memory_adapter
        self._folder = Path(folder)
        self._launch = launch or (lambda program, arguments, cwd: launch_elevated(program, arguments, cwd))
        self._accept_seconds = accept_seconds
        self._program = program
        self._memory_adapter = memory_adapter
        self._stop = threading.Event()
        self._sock = None
        self._outbox = queue.Queue(maxsize=OUTBOX)
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

    # --- what the mesh calls ------------------------------------------------------------------------------------

    def set_peers(self, peers):
        """The members, [{"key", "address"}], other than this PC. The helper adds and removes WireGuard peers to match."""
        self._peers = [{"key": p["key"], "address": p["address"]} for p in peers]
        self._put(frame("C", json.dumps(self._config()).encode("utf-8")))

    def set_open_services(self, groups):
        """Which groups of Windows services members may reach on this PC. Takes effect at once in the helper."""
        self._open = sorted(groups)
        self._put(frame("C", json.dumps(self._config()).encode("utf-8")))

    def deliver(self, member, datagram):
        """Hand the helper a datagram that arrived on the path from `member` (their address's last byte)."""
        return self._put(frame("D", bytes([member]), datagram))

    def path_up(self, member):
        """A way to `member` started working; WireGuard starts its handshake at once instead of after its retry."""
        return self._put(frame("U", bytes([member])))

    def put_packet(self, packet):
        """Tests, with memory_adapter: a packet "Windows" sends on the adapter."""
        return self._put(frame("T", packet))

    def _config(self):
        return {"private_key": self._private_key, "address": self.address, "network": self.network,
                "peers": self._peers, "open": self._open}

    def _put(self, data):
        if self._sock is None:
            return False
        try:
            self._outbox.put_nowait(data)
            return True
        except queue.Full:
            return False

    # --- the helper ---------------------------------------------------------------------------------------------

    def _set(self, state, message=""):
        self.status = {"state": state, "message": message}

    def _run(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Without it, another program could bind the same port too and be handed the helper and its secret.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1.0)
        port = listener.getsockname()[1]
        secret = secrets.token_hex(32)
        self._folder.mkdir(parents=True, exist_ok=True)
        secret_file = self._folder / f"net-{secrets.token_hex(6)}.key"
        secret_file.write_text(secret, encoding="ascii")
        try:
            program, arguments, cwd = helper_command(port, secret_file, os.getpid(), self._program, self._memory_adapter)
            if not Path(program).exists():
                self._set("failed", "The game network helper is missing from this copy of PrivatePartyNet.")
                return
            self._set("asking", "Windows is asking for permission to add the game network adapter.")
            problem = self._launch(program, arguments, cwd)
            if problem:
                self._set("declined" if problem == "declined" else "failed",
                          "Permission was not given, so the game network is off." if problem == "declined"
                          else f"The game network helper {problem}.")
                return
            conn = self._accept(listener, secret)
            try:
                secret_file.unlink()  # used once; the helper only reads it
            except OSError:
                pass
            if conn is None:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.sendall(frame("C", json.dumps(self._config()).encode("utf-8")))
            self._sock = conn
            self._set("starting", "Setting up the game network adapter...")
            writer = threading.Thread(target=self._write, args=(conn,), name="privatepartynet-game-out", daemon=True)
            writer.start()
            self._pump(conn)
        finally:
            listener.close()
            try:
                secret_file.unlink()
            except OSError:
                pass
            if self.status["state"] in ("on", "starting"):
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
                try:
                    conn.sendall(app_proof(secret) + b"\n")  # and the app shows the helper it is the app
                except OSError:
                    conn.close()
                    continue
                conn.settimeout(None)
                return conn
            conn.close()  # not our helper: keep waiting for the real one
        if not self._stop.is_set():
            self._set("failed", "The game network helper did not start in time.")
        return None

    def _write(self, conn):
        while not self._stop.is_set() and self._sock is conn:
            try:
                data = self._outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            parts = [data]
            while len(parts) < 64:  # a burst goes out in one write
                try:
                    parts.append(self._outbox.get_nowait())
                except queue.Empty:
                    break
            try:
                conn.sendall(b"".join(parts))
            except OSError:
                return

    def _pump(self, conn):
        try:
            while not self._stop.is_set():
                kind, body = read_frame(conn)
                if kind == "D" and len(body) > 1:
                    self.on_datagram(body[0], body[1:])
                elif kind == "S":
                    status = json.loads(body)
                    if status.get("state") == "on":
                        # What Windows would not do (keep the network Private, let another adapter step aside), in
                        # the helper's plain words: the game network works, but the person should know.
                        warnings = [str(w)[:300] for w in status.get("warnings") or [] if isinstance(w, str)]
                        self.status = {"state": "on", "message": " ".join([f"Game network on: this PC is {self.address}.",
                                                                          *warnings]), "warnings": warnings}
                        for warning in warnings:
                            log.warning("Game network: %s", warning)
                    else:
                        self._set("failed", status.get("message") or "The game network helper stopped.")
                elif kind == "L":
                    log.info("Game network helper: %s", body.decode("utf-8", "replace")[:500])
                elif kind == "T" and self._on_packet is not None:
                    self._on_packet(body)
        except (EOFError, OSError, ValueError, struct.error):
            pass


class GameMesh:
    """Follows the host's list of who is on: a path to each other member, and the helper told who they are.

    `paths` is a PathManager (pathway.py) or anything with its path_to(peer_id, candidates)."""

    def __init__(self, user_id, paths, session):
        self.user_id = user_id
        self._paths = paths
        self.session = session
        session.on_datagram = self.on_datagram
        self._lock = threading.Lock()
        self._members = {}  # member byte -> {"user", "address", "key", "path"}

    def on_datagram(self, member, datagram):
        """WireGuard has a datagram for `member`: send it along their path."""
        entry = self._members.get(member)
        if entry is None:
            return
        path = entry["path"]
        if not path.kind and entry["starts"] and datagram[:1] == HANDSHAKE_START:
            entry["held"] = datagram  # no way yet: kept for when one starts working (see _path_changed)
        path.send(datagram)

    def _path_changed(self, member, kind):
        """A way to `member` started working, or moved. WireGuard waits five seconds between attempts at a handshake, so
        the attempt dropped while there was no way is sent now, and the helper is told to try again at once."""
        entry = self._members.get(member)
        if entry is None or not kind:
            return
        held, entry["held"] = entry["held"], None
        if held is not None:
            entry["path"].send(held)
        self.session.path_up(member)

    def apply(self, roster):
        """Make the members match `roster` (as gamenet.open_roster gives it): new paths for newcomers, paths closed for
        those who left. Returns the members, [{"user", "address", "key", "kind"}]."""
        wanted = {}
        for peer in roster["peers"]:
            if peer["user"] != self.user_id:
                wanted[member_byte(peer["address"])] = peer
        closing = []
        with self._lock:
            for member, entry in list(self._members.items()):
                peer = wanted.get(member)
                if peer is None or peer["user"] != entry["user"] or peer["key"] != entry["key"]:
                    closing.append(entry["path"])
                    del self._members[member]
            for member, peer in wanted.items():
                path = self._paths.path_to(peer["user"], peer["candidates"])  # newer candidates update the same path
                if member not in self._members:
                    path.set_receiver(lambda datagram, m=member: self.session.deliver(m, datagram))
                    path.set_on_change(lambda kind, m=member: self._path_changed(m, kind))
                    # Only the lower address of each pair starts handshakes (as in the helper): when both start at
                    # once, each one's answer cancels the other's attempt.
                    starts = ipaddress.IPv4Address(self.session.address) < ipaddress.IPv4Address(peer["address"])
                    self._members[member] = {"user": peer["user"], "address": peer["address"], "key": peer["key"],
                                             "path": path, "starts": starts, "held": None}
                    if path.kind:
                        self.session.path_up(member)
            peers = [{"key": e["key"], "address": e["address"]} for e in self._members.values()]
        for path in closing:
            path.close()
        self.session.set_peers(peers)
        return self.members()

    def members(self):
        with self._lock:
            return [{"user": e["user"], "address": e["address"], "key": e["key"], "kind": e["path"].kind}
                    for _, e in sorted(self._members.items())]

    def stop(self):
        with self._lock:
            paths = [e["path"] for e in self._members.values()]
            self._members.clear()
        for path in paths:
            path.close()
