"""A PC's side of a relay (relayproto.py): keep channels bound, send and receive through them, and fall back to TLS on
TCP port 443 when a network lets no UDP out.

RelayClient talks to one relay. Its owner either lends it a UDP socket (the path manager, whose one socket also serves
as a friend relay and for direct paths) and feeds it what arrives from the relay, or lets a RelayBridge own one.
RelayBridge carries relay channels to UDP ports on this PC, so a program that only speaks UDP (the QUIC join tunnel in
punch.py) works through a relay unchanged.
"""

import hashlib
import logging
import queue
import select
import socket
import ssl
import struct
import threading
import time

from privatepartynet import relayproto as rp

log = logging.getLogger("privatepartynet.relay")

FALLBACK_SECONDS = 4.0  # no answer over UDP for this long, and the client tries TLS instead
TLS_QUEUE = 512
LENGTH = struct.Struct("!H")


def resolve(host, port):
    """Every (ip, port) a relay's name or address has, IPv6 and IPv4 taken in turn; [] when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
    except (OSError, UnicodeError):
        return []
    v6 = [(i[4][0].split("%")[0], i[4][1]) for i in infos if i[0] == socket.AF_INET6]
    v4 = [(i[4][0], i[4][1]) for i in infos if i[0] == socket.AF_INET]
    found = []
    for pair in zip(v6 + [None] * len(v4), v4 + [None] * len(v6)):
        found += [a for a in pair if a is not None and a not in found]
    return found


HEARD_SECONDS = 3 * 15.0  # no word from the address in use for this long, and every address is tried again


class TlsLink:
    """Relay messages over one TLS connection, each preceded by its length. send() never blocks; a full queue drops.

    `pin` (hex SHA-256 of the relay's certificate) is checked. The relay's certificate is its own, self-made one; the
    pin is what proves it. RelayClient never opens one without a pin."""

    def __init__(self, host, port, pin, on_message, timeout=5.0):
        self.host, self.port, self.pin = host, port, (pin or "").lower()
        self.on_message = on_message
        self._timeout = timeout
        self._queue = queue.Queue(maxsize=TLS_QUEUE)
        self._sock = None
        self.alive = True
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._run, name="privatepartynet-relay-tls", daemon=True)
        self._thread.start()

    def send(self, data):
        if not self.alive:
            return False
        try:
            self._queue.put_nowait(bytes(data))
            return True
        except queue.Full:
            return False

    def _run(self):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self._timeout)
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # replaced by the pin just below
            sock = context.wrap_socket(raw, server_hostname=self.host if not _is_ip(self.host) else None)
            der = sock.getpeercert(binary_form=True) or b""
            if self.pin and hashlib.sha256(der).hexdigest() != self.pin:
                sock.close()
                raise rp.RelayError("the relay's certificate is not the one in the settings")
            sock.settimeout(None)
            self._sock = sock
        except (OSError, rp.RelayError) as exc:
            log.info("Relay %s:%s over TLS did not connect: %s", self.host, self.port, exc)
            self.alive = False
            return
        self.connected.set()
        writer = threading.Thread(target=self._write, name="privatepartynet-relay-tls-out", daemon=True)
        writer.start()
        try:
            reader = sock.makefile("rb")
            while self.alive:
                head = reader.read(2)
                if len(head) < 2:
                    break
                body = reader.read(LENGTH.unpack(head)[0])
                if body:
                    self.on_message(body)
        except (OSError, ValueError):
            pass
        finally:
            self.alive = False
            self._queue.put(None)
            try:
                sock.close()
            except OSError:
                pass

    def _write(self):
        while self.alive:
            item = self._queue.get()
            if item is None:
                break
            try:
                self._sock.sendall(LENGTH.pack(len(item)) + item)
            except OSError:
                self.alive = False
                break

    def close(self):
        self.alive = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _is_ip(text):
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in text else socket.AF_INET, text)
        return True
    except OSError:
        return False


class RelayClient:
    """One relay, seen from this PC: the channels it should keep bound, and the way to it (UDP, else TLS).

    `relay` is {"host", "port", "tls_port"?, "pin"?, "credential"}, where the credential makes BINDs
    (relayproto.HmacCredential for a VPS relay, pathway.PassCredential for a friend relay). `udp_send(data, address)`
    sends from the owner's socket; the owner passes everything that arrives from `address` to feed().
    `on_deliver(channel, payload)` gets each datagram from the other end of a channel. Call tick() a few times a second.

    A relay's name may have several addresses, IPv6 and IPv4. Until one answers, BINDs go to all of them, and the first
    to answer is used from then on ("happy eyeballs"), so a member with only IPv6 or only IPv4 reaches a relay that
    has both. When the one in use falls silent, all are tried again."""

    def __init__(self, relay, on_deliver, udp_send, clock=time.monotonic, resolver=resolve):
        self.relay = dict(relay)
        self.on_deliver = on_deliver
        self._udp_send = udp_send
        self._clock = clock
        self.address = None  # the address in use, once one answered
        self.addresses = []  # every address the relay's name has
        self._last_heard = None
        self.tls = None
        self.channels = {}  # channel -> {"credential", "next", "status", "cookie"}
        self._lock = threading.Lock()
        self._first_ask = None
        self._heard = False
        self.closed = False
        self._resolver = resolver
        threading.Thread(target=self._resolve, name="privatepartynet-relay-resolve", daemon=True).start()

    @property
    def key(self):
        return (self.relay["host"], int(self.relay["port"]))

    @property
    def transport(self):
        return "tls" if self.tls is not None else "udp"

    def _resolve(self):
        self.addresses = [tuple(a[:2]) for a in self._resolver(self.relay["host"], int(self.relay["port"]))]
        if not self.addresses:
            log.info("The relay %s did not resolve", self.relay["host"])
        elif len(self.addresses) == 1:
            self.address = self.addresses[0]

    def matches(self, source):
        """True when `source` is one of this relay's addresses."""
        return tuple(source[:2]) in self.addresses

    def want(self, channel, credential=None):
        """Keep `channel` bound on this relay, with `credential` (or the relay's own)."""
        with self._lock:
            entry = self.channels.get(channel)
            if entry is None:
                self.channels[channel] = {"credential": credential or self.relay["credential"], "next": 0.0,
                                          "status": None, "cookie": b""}
            elif credential:
                entry["credential"] = credential

    def drop(self, channel):
        with self._lock:
            self.channels.pop(channel, None)

    def is_bound(self, channel):
        entry = self.channels.get(channel)
        return bool(entry and entry["status"] == rp.OK)

    def refused(self, channel):
        entry = self.channels.get(channel)
        return entry["status"] if entry and entry["status"] not in (None, rp.OK, rp.CHECK) else None

    def _out(self, data):
        if self.tls is not None:
            return self.tls.send(data)
        if self.address is not None:
            return self._udp_send(data, self.address)
        sent = False
        for address in self.addresses:
            sent = self._udp_send(data, address) or sent
        return sent

    def send(self, channel, payload):
        return self._out(rp.send(channel, payload))

    def tick(self, now=None):
        if self.closed:
            return
        now = self._clock() if now is None else now
        # TLS only with the relay's certificate pinned: without the pin anyone in between could stand in for it.
        if self.tls is None and not self._heard and self._first_ask is not None and self.relay.get("tls_port") \
                and self.relay.get("pin") and now - self._first_ask > FALLBACK_SECONDS:
            log.info("Relay %s answers nothing over UDP; trying TLS on port %s", self.relay["host"], self.relay["tls_port"])
            self.tls = TlsLink(self.relay["host"], int(self.relay["tls_port"]), self.relay.get("pin", ""), self.feed)
            with self._lock:
                for entry in self.channels.values():
                    entry["next"] = 0.0
        if self.tls is not None and not self.tls.alive:
            self.tls = None  # it failed or dropped: back to UDP, and to TLS again after another silence
            self._first_ask, self._heard = now, False
        if self.address is not None and len(self.addresses) > 1 and self._last_heard is not None \
                and now - self._last_heard > HEARD_SECONDS:
            self.address = None  # the network may have changed under us: let every address race again
        with self._lock:
            due = [(c, e) for c, e in self.channels.items() if e["next"] <= now]
            for channel, entry in due:
                retry = rp.REBIND_SECONDS if entry["status"] == rp.OK else 1.0
                entry["next"] = now + (retry if entry["status"] in (None, rp.OK, rp.CHECK) else 30.0)
        for channel, entry in due:
            self._bind(channel, entry, now)

    def _bind(self, channel, entry, now):
        try:
            message = entry["credential"].bind(channel, entry["cookie"])
        except (ValueError, AttributeError):
            return
        entry["cookie"] = b""  # a cookie is good for one address and half a minute; the relay hands out another
        if self._out(message) and self._first_ask is None:
            self._first_ask = now

    def feed(self, data, source=None):
        """A message from the relay; `source` is the address it came from (None over TLS)."""
        message = rp.parse(data)
        if message is None:
            return
        kind, channel, rest = message
        self._heard = True
        self._last_heard = self._clock()
        if source is not None and self.address is None and self.matches(source):
            self.address = tuple(source[:2])  # the first address to answer is the one used
        if kind == rp.BOUND:
            with self._lock:
                entry = self.channels.get(channel)
                if entry is None:
                    return
                if rest[0] == rp.CHECK and len(rest) == 1 + rp.COOKIE_BYTES:
                    # The relay wants this address proved: answer at once with its cookie.
                    if entry["status"] != rp.OK:
                        entry["status"] = rp.CHECK
                    entry["cookie"] = rest[1:]
                    entry["next"] = self._clock() + 1.0
                    answer = True
                else:
                    entry["status"] = rest[0]
                    answer = False
                    if rest[0] != rp.OK:
                        log.info("Relay %s: %s", self.relay["host"], rp.STATUS_TEXT.get(rest[0], "refused"))
            if answer:
                self._bind(channel, entry, self._clock())
        elif kind == rp.DELIVER and channel in self.channels:
            self.on_deliver(channel, rest)

    def close(self):
        self.closed = True
        if self.tls is not None:
            self.tls.close()


class RelayBridge:
    """Relay channels carried to UDP ports on this PC.

    For each channel added, a loopback UDP socket stands for the other end: what a program sends to it goes through
    the relay, and what comes back is sent to `target` (a fixed address, such as a QUIC server) or else to wherever the
    program last sent from. `port_for(channel)` is that socket's port."""

    def __init__(self, relay, clock=time.monotonic, resolver=resolve):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", 0))
        self._sock.setblocking(False)
        self._sock6 = None
        try:
            sock6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock6.bind(("::", 0))
            sock6.setblocking(False)
            self._sock6 = sock6
        except OSError:
            pass  # no IPv6 on this PC
        self.client = RelayClient(relay, self._delivered, self._udp_send, clock=clock, resolver=resolver)
        self._locals = {}  # channel -> {"sock", "target", "peer"}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="privatepartynet-relay-bridge", daemon=True)
        self._thread.start()

    def _udp_send(self, data, address):
        sock = self._sock6 if ":" in address[0] else self._sock
        if sock is None:
            return False
        try:
            sock.sendto(data, address[:2])
            return True
        except OSError:
            return False

    def add(self, channel, credential=None, target=None):
        """Bind `channel`; returns the loopback port that stands for its other end."""
        with self._lock:
            entry = self._locals.get(channel)
            if entry is None:
                local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                local.bind(("127.0.0.1", 0))
                local.setblocking(False)
                entry = self._locals[channel] = {"sock": local, "target": target, "peer": None}
        self.client.want(channel, credential)
        return entry["sock"].getsockname()[1]

    def remove(self, channel):
        self.client.drop(channel)
        with self._lock:
            entry = self._locals.pop(channel, None)
        if entry is not None:
            entry["sock"].close()

    def port_for(self, channel):
        entry = self._locals.get(channel)
        return entry["sock"].getsockname()[1] if entry else None

    def wait_bound(self, channel, seconds):
        """True once the relay said yes for `channel`; raises RelayError when it said no."""
        ends = time.monotonic() + seconds
        while time.monotonic() < ends and not self._stop.is_set():
            if self.client.is_bound(channel):
                return True
            status = self.client.refused(channel)
            if status is not None:
                raise rp.RelayError(rp.STATUS_TEXT.get(status, "the relay refused"))
            time.sleep(0.05)
        return False

    def _delivered(self, channel, payload):
        entry = self._locals.get(channel)
        if entry is None:
            return
        destination = entry["target"] or entry["peer"]
        if destination is not None:
            try:
                entry["sock"].sendto(payload, destination)
            except OSError:
                pass

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                watched = {entry["sock"]: channel for channel, entry in self._locals.items()}
            try:
                ready, _, _ = select.select([s for s in (self._sock, self._sock6) if s] + list(watched), [], [], 0.1)
            except (OSError, ValueError):
                ready = []
            for sock in ready:
                try:
                    data, source = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is self._sock or sock is self._sock6:
                    if self.client.matches(source):
                        self.client.feed(data, source)
                    continue
                channel = watched[sock]
                entry = self._locals.get(channel)
                if entry is not None and entry["target"] is None:
                    entry["peer"] = source
                if len(data) <= rp.MAX_PAYLOAD:
                    self.client.send(channel, data)
            self.client.tick()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.client.close()
        with self._lock:
            for entry in self._locals.values():
                entry["sock"].close()
            self._locals.clear()
        self._sock.close()
        if self._sock6 is not None:
            self._sock6.close()
