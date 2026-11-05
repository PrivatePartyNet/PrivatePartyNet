"""A joined party window's own address on this PC, which stays the same however the host is reached.

A party window used to load the party straight from the way its join found: the host's address, or a punched tunnel's
port on this PC. When that way broke (Starlink dropping out, a new public address, a tunnel that timed out) the window
had nowhere to go back to. Now the window loads the party from https://127.x.y.z:port, a PartyLink on this PC, which
carries each connection to the host by the way that works at the moment, byte for byte (the TLS inside is the page's
own, still checked against the host's certificate, windowpins.py). When a connection to the host cannot be made, the
link finds the host again, trying the old way first, then the ladder (connect.py, punch.py: the host's addresses,
IPv6, punching through both routers, a relay), with growing pauses (BACKOFF, with jitter) for as long as the reconnect
grace lasts. The page's own cookie, origin and peer connections stay as they were.

Each link listens on its own loopback address (127.84.a.b), so two parties' cookies never mix (cookies do not look at
the port).

A way is a function that returns (host, port): a TCP address that reaches the party now (the host itself, or the local
port of a punched or relayed tunnel). The app gives the link a `find(force)` that returns the current way, or with
force a fresh one.
"""

import itertools
import logging
import random
import socket
import threading
import time

log = logging.getLogger("privatepartynet.partylink")

BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0, 10.0)  # then 10 s each time
CONNECT_SECONDS = 5.0
_addresses = itertools.count(1)


def backoff(attempt, rng=random.random):
    """The pause before try number `attempt` (0, 1, ...): 0.5 s, 1, 2, 4, 8, then 10 s, each with +-20% jitter."""
    base = BACKOFF[min(attempt, len(BACKOFF) - 1)]
    return base * (0.8 + 0.4 * rng())


def next_address():
    n = next(_addresses)
    return f"127.84.{(n // 250) % 250}.{n % 250 + 1}"


class LinkLost(Exception):
    """No way to the host within the grace."""


class PartyLink:
    def __init__(self, find, name="the party", grace=120.0, address=None, on_change=None, connect_seconds=CONNECT_SECONDS):
        """`find(force)` gives (host, port) for the host now: the last way that worked, or with force=True a freshly
        found one (it may take a while: a punch through two routers takes up to two minutes). It raises when it finds
        none. `on_change(state, message)` hears "ok", "reconnecting" and "lost"."""
        self._find = find
        self.name = name
        self.grace = grace
        self._connect_seconds = connect_seconds
        self._on_change = on_change or (lambda state, message: None)
        self._lock = threading.Lock()
        self._finding = threading.Lock()  # one search at a time; connections that arrive meanwhile wait for it
        self._open = set()  # (client, upstream) sockets being carried, closed when the network changes
        self._stop = threading.Event()
        self._way = None
        self.state, self.message, self.since = "ok", "", time.time()
        self.lost_since = None
        self._sock = None
        for _ in range(20):
            candidate = address or next_address()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((candidate, 0))
            except OSError:
                sock.close()
                if address:
                    raise
                continue
            self._sock = sock
            break
        if self._sock is None:
            raise OSError("no loopback address for the party link")
        self._sock.listen(64)
        self.address, self.port = self._sock.getsockname()[:2]
        self._thread = threading.Thread(target=self._accept, name="privatepartynet-partylink", daemon=True)
        self._thread.start()

    @property
    def base(self):
        return f"https://{self.address}:{self.port}"

    @property
    def way(self):
        return self._way

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self.network_changed()

    def network_changed(self):
        """This PC's network changed (another Wi-Fi, a new address, a VPN up or down): drop every connection now, so
        the page and the game network reconnect at once instead of waiting for a timeout, and look the way up again."""
        with self._lock:
            pairs, self._open = list(self._open), set()
        for pair in pairs:
            for sock in pair:
                _close(sock)
        if not self._stop.is_set() and pairs:
            log.info("The network changed; reconnecting to %s", self.name)

    def _set(self, state, message=""):
        if state == self.state and message == self.message:
            return
        self.state, self.message, self.since = state, message, time.time()
        self.lost_since = None if state == "ok" else (self.lost_since or time.monotonic())
        try:
            self._on_change(state, message)
        except Exception:
            log.exception("A party link listener failed")

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._carry, args=(client,), name="privatepartynet-partylink-conn",
                             daemon=True).start()

    def connect(self):
        """A socket to the host by a way that works, finding the host again (with growing pauses) when the way it had
        fails. Raises LinkLost once the grace has run out."""
        failed = None
        if self.state == "ok":
            try:
                way = self._find(False)  # the current way, checked (a tunnel's local port answers even when it is dead)
                failed = way
                sock = socket.create_connection(way, timeout=self._connect_seconds)
                self._way = way
                return sock, way
            except Exception as exc:
                log.info("%s did not answer by its way (%s); finding it again", self.name, exc)
        with self._finding:
            started = self.lost_since or time.monotonic()
            self._set("reconnecting", f"Connection to {self.name} lost; reconnecting")
            for attempt in itertools.count():
                if self._stop.is_set():
                    raise LinkLost("closed")
                # First the way as it is (another connection may have found a new one meanwhile, or the dropout was
                # short), then a fresh one.
                for force in ((False, True) if attempt == 0 else (True,)):
                    try:
                        way = self._find(force)
                        if not force and way == failed:
                            continue  # the way that just failed, and nobody found another meanwhile
                        sock = socket.create_connection(way, timeout=self._connect_seconds)
                    except Exception as exc:
                        log.info("Reaching %s again, try %d%s: %s", self.name, attempt + 1,
                                 " (looking for a new way)" if force else "", exc)
                        continue
                    if way != self._way:
                        log.info("%s is reached again at %s port %s", self.name, way[0], way[1])
                    self._way = way
                    self._set("ok")
                    return sock, way
                if time.monotonic() - started >= self.grace:
                    self._set("lost", f"The connection to {self.name} could not be restored.")
                    raise LinkLost(self.message)
                self._stop.wait(backoff(attempt))
        raise LinkLost("closed")

    def _carry(self, client):
        try:
            upstream, _ = self.connect()
        except (LinkLost, OSError):
            _close(client)
            return
        pair = (client, upstream)
        with self._lock:
            self._open.add(pair)
        for sock in pair:
            sock.settimeout(None)
        ends = []  # the directions that finished; the pair is closed once both have (as TCP does, half at a time)
        ends_lock = threading.Lock()

        def pipe(source, target):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    target.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                with ends_lock:
                    ends.append(source)
                    last = len(ends) == 2
                if last:
                    for sock in pair:
                        _close(sock)
                    with self._lock:
                        self._open.discard(pair)

        threading.Thread(target=pipe, args=(upstream, client), name="privatepartynet-partylink-down",
                         daemon=True).start()
        pipe(client, upstream)


def _close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
