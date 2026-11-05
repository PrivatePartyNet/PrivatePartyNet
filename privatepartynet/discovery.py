"""Finds other PCs running PrivatePartyNet on the same network, and sends them connection requests.

Every open copy broadcasts a small UDP beacon: its name, whether it is sharing, and the port to reach it on.
Beacons repeat every few seconds, and go out at once whenever the name or the sharing state changes. A PC that
falls silent drops off the list after STALE_SECONDS, and a PC that closes says goodbye.

A connection request never starts a share on its own. The other person has to say yes, and only then does
their copy start sharing and reply with the address to open.

Everything here is local network traffic. Nothing reaches the internet, so it works offline.
"""

import json
import queue
import socket
import threading
import time
import uuid

from privatepartynet.identity import user_id_of
from privatepartynet.netinfo import LOOPBACK, lan_addresses

DEFAULT_PORT = 8766
BEACON_SECONDS = 2.0
STALE_SECONDS = 7.0
REQUEST_TIMEOUT = 30.0
MAX_NAME = 40
MAX_DATAGRAM = 8192  # an accept carries the party's certificate, about 1.5 KB
TAG = "privatepartynet"
KINDS = {"beacon", "bye", "request", "accept", "decline"}


def clean_name(value):
    """A display name: printable characters only, trimmed, at most MAX_NAME long. Blank gives the PC name."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:MAX_NAME] or socket.gethostname()[:MAX_NAME]


def broadcast_targets(port):
    """Where a beacon goes: each LAN address's /24 broadcast, plus the limited broadcast address."""
    targets = {(address.rsplit(".", 1)[0] + ".255", port) for address in lan_addresses() if address != LOOPBACK}
    targets.add(("255.255.255.255", port))
    return sorted(targets)


class Discovery:
    """One instance per open copy. Start it, read events from .events, and stop it on exit.

    Events are (name, data) tuples on the queue: "peers" (the full list), "request", "accepted", "declined",
    "timeout" and "error". The GUI thread reads them, so this class never touches Tk.
    """

    def __init__(self, name, port=DEFAULT_PORT, targets=None, instance_id=None, beacon_seconds=BEACON_SECONDS):
        self.id = instance_id or uuid.uuid4().hex
        self.name = clean_name(name)
        self.port = port
        self.targets = targets  # None means the LAN broadcast addresses; tests pass explicit ones
        self.beacon_seconds = beacon_seconds
        self.events = queue.Queue()
        self._peers = {}  # id -> record
        self._outgoing = {}  # id -> (address, time sent)
        self._incoming = {}  # id -> address
        self._sharing = (None, None)  # (port, mode) while this copy is sharing
        self._who = None  # (user id, sign key hex, dh key hex, direct message port) once the identity is open
        self._fingerprint = self._spki = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = None
        self._threads = []

    def start(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.bind(("0.0.0.0", self.port))
        self._socket.settimeout(0.5)
        self.port = self._socket.getsockname()[1]
        for target in (self._receive_loop, self._beacon_loop):
            thread = threading.Thread(target=target, name=f"privatepartynet-{target.__name__}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._send_beacon("beacon")

    def stop(self):
        if self._socket is None:
            return
        self._send_beacon("bye")
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._socket.close()
        self._socket = None

    def set_name(self, name):
        with self._lock:
            self.name = clean_name(name)
        self._send_beacon("beacon")

    def set_sharing(self, port=None, mode=None, fingerprint=None, spki=None):
        """Say whether a party is open here. `fingerprint` (the party certificate's SHA-256, "fp", which 3.0/3.1 check)
        and `spki` (its public key's, which outlasts a renewal) let a PC that asks to join check it reached this PC and
        not another one claiming the same name."""
        with self._lock:
            self._sharing = (port, mode) if port else (None, None)
            self._fingerprint = fingerprint if port else None
            self._spki = spki if port else None
        self._send_beacon("beacon")

    def set_identity(self, user, sign, dh, dm_port):
        """Announce who this copy is: its permanent id, its public keys and the port that takes direct messages."""
        with self._lock:
            self._who = (user, sign, dh, dm_port)
        self._send_beacon("beacon")

    def peer_list(self):
        """Peers heard from recently, sorted by name. Each is a dict for the window to show."""
        with self._lock:
            peers = [dict(record) for record in self._peers.values()]
        return sorted(peers, key=lambda peer: (peer["name"].lower(), peer["id"]))

    def request(self, peer_id):
        """Ask a peer to accept a connection. Returns False when that peer is not known."""
        with self._lock:
            record = self._peers.get(peer_id)
            if record is None:
                return False
            self._outgoing[peer_id] = (record["_address"], time.monotonic())
        self._send("request", record["_address"])
        return True

    def answer(self, peer_id, accept, url=None, cert=None):
        """Reply to an incoming request. url is the address the requester should open when accepting."""
        with self._lock:
            address = self._incoming.pop(peer_id, None)
        if address is None:
            return False
        if accept:
            self._send("accept", address, url=url, cert=cert)
        else:
            self._send("decline", address)
        return True

    def _envelope(self, kind, **extra):
        with self._lock:
            body = {"tag": TAG, "kind": kind, "id": self.id, "name": self.name}
            if kind == "beacon":
                port, mode = self._sharing
                body.update(sharing=port is not None, port=port, mode=mode)
                if self._fingerprint:
                    body["fp"] = self._fingerprint
                if self._spki:
                    body["spki"] = self._spki
                if self._who is not None:
                    body.update(user=self._who[0], sign=self._who[1], dh=self._who[2], dm=self._who[3])
        body.update(extra)
        return json.dumps(body).encode("utf-8")

    def _send(self, kind, address, **extra):
        if self._socket is None:
            return
        try:
            self._socket.sendto(self._envelope(kind, **extra), address)
        except OSError as exc:
            self.events.put(("error", f"Could not send to {address[0]}: {exc}"))

    def _send_beacon(self, kind):
        if self._socket is None:
            return
        payload = self._envelope(kind)
        for address in self.targets if self.targets is not None else broadcast_targets(self.port):
            try:
                self._socket.sendto(payload, address)
            except OSError:
                pass  # a network with no broadcast route hears nothing; the next beacon tries again

    def _beacon_loop(self):
        while not self._stop.wait(self.beacon_seconds):
            self._send_beacon("beacon")
            self._expire()

    def _receive_loop(self):
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break  # the socket was closed by stop()
            message = _parse(data)
            if message is None or message["id"] == self.id:
                continue
            self._handle(message, address)

    def _handle(self, message, address):
        kind, peer_id, name = message["kind"], message["id"], message["name"]
        if kind == "beacon":
            who = _who(message)
            with self._lock:
                self._peers[peer_id] = {
                    "user": who[0] if who else None,
                    "sign": who[1] if who else None,
                    "dh": who[2] if who else None,
                    "dm": who[3] if who else None,
                    "fp": message.get("fp") if _hex64(message.get("fp")) else None,
                    "spki": message.get("spki") if _hex64(message.get("spki")) else None,
                    "id": peer_id,
                    "name": name,
                    "address": address[0],
                    "port": message.get("port"),
                    "sharing": bool(message.get("sharing")),
                    "mode": message.get("mode"),
                    "seen": time.monotonic(),
                    "_address": address,
                }
            self._emit_peers()
        elif kind == "bye":
            with self._lock:
                removed = self._peers.pop(peer_id, None)
            if removed is not None:
                self._emit_peers()
        elif kind == "request":
            with self._lock:
                self._incoming[peer_id] = address
            self.events.put(("request", {"id": peer_id, "name": name, "address": address[0]}))
        elif kind == "accept":
            with self._lock:
                self._outgoing.pop(peer_id, None)
            url = message.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                cert = message.get("cert")
                if not isinstance(cert, str) or len(cert) > 8192 or not cert.startswith("-----BEGIN CERTIFICATE-----"):
                    cert = ""
                self.events.put(("accepted", {"id": peer_id, "name": name, "url": url, "cert": cert}))
        elif kind == "decline":
            with self._lock:
                self._outgoing.pop(peer_id, None)
            self.events.put(("declined", {"id": peer_id, "name": name}))

    def _expire(self):
        now = time.monotonic()
        with self._lock:
            stale = [peer_id for peer_id, record in self._peers.items() if now - record["seen"] > STALE_SECONDS]
            for peer_id in stale:
                del self._peers[peer_id]
            timed_out = [
                (peer_id, record[0]) for peer_id, record in self._outgoing.items() if now - record[1] > REQUEST_TIMEOUT
            ]
            for peer_id, _ in timed_out:
                del self._outgoing[peer_id]
        if stale:
            self._emit_peers()
        for peer_id, address in timed_out:
            self.events.put(("timeout", {"id": peer_id, "address": address[0]}))

    def _emit_peers(self):
        self.events.put(("peers", self.peer_list()))


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _who(message):
    """(user id, sign, dh, direct message port) from a beacon, only when the id really is the hash of the two keys.
    A beacon that names someone else's id with other keys is ignored, so a listing cannot pose as another person."""
    user, sign, dh, port = message.get("user"), message.get("sign"), message.get("dh"), message.get("dm")
    if not (_hex64(sign) and _hex64(dh)) or user_id_of(sign, dh) != user:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        port = None
    return user, sign, dh, port


def _parse(data):
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict) or message.get("tag") != TAG or message.get("kind") not in KINDS:
        return None
    peer_id = message.get("id")
    if not isinstance(peer_id, str) or not 1 <= len(peer_id) <= 64:
        return None
    message["name"] = clean_name(message.get("name"))
    return message
