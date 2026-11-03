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
    """(ip, port) for a relay's name or address, IPv4 first. None when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
    except (OSError, UnicodeError):
        return None
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    return (infos[0][4][0], infos[0][4][1]) if infos else None


class TlsLink:
    """Relay messages over one TLS connection, each preceded by its length. send() never blocks; a full queue drops.

    `pin` (hex SHA-256 of the relay's certificate) is checked when given. The relay's certificate is usually its own,
    self-made one; the pin is what proves it, and what it carries is end-to-end encrypted either way."""

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
    `on_deliver(channel, payload)` gets each datagram from the other end of a channel. Call tick() a few times a second."""

    def __init__(self, relay, on_deliver, udp_send, clock=time.monotonic, resolver=resolve):
        self.relay = dict(relay)
        self.on_deliver = on_deliver
        self._udp_send = udp_send
        self._clock = clock
        self.address = None
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
        self.address = self._resolver(self.relay["host"], int(self.relay["port"]))
        if self.address is None:
            log.info("The relay %s did not resolve", self.relay["host"])

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
        if self.address is None:
            return False
        return self._udp_send(data, self.address)

    def send(self, channel, payload):
        return self._out(rp.send(channel, payload))

    def tick(self, now=None):
        if self.closed:
            return
        now = self._clock() if now is None else now
        if self.tls is None and not self._heard and self._first_ask is not None and self.relay.get("tls_port") \
                and now - self._first_ask > FALLBACK_SECONDS:
            log.info("Relay %s answers nothing over UDP; trying TLS on port %s", self.relay["host"], self.relay["tls_port"])
            self.tls = TlsLink(self.relay["host"], int(self.relay["tls_port"]), self.relay.get("pin", ""), self.feed)
            with self._lock:
                for entry in self.channels.values():
                    entry["next"] = 0.0
        if self.tls is not None and not self.tls.alive:
            self.tls = None  # it failed or dropped: back to UDP, and to TLS again after another silence
            self._first_ask, self._heard = now, False
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

    def feed(self, data):
        """A message from the relay."""
        message = rp.parse(data)
        if message is None:
            return
        kind, channel, rest = message
        self._heard = True
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
        self.client = RelayClient(relay, self._delivered, self._udp_send, clock=clock, resolver=resolver)
        self._locals = {}  # channel -> {"sock", "target", "peer"}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="privatepartynet-relay-bridge", daemon=True)
        self._thread.start()

    def _udp_send(self, data, address):
        try:
            self._sock.sendto(data, address)
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
                ready, _, _ = select.select([self._sock, *watched], [], [], 0.1)
            except (OSError, ValueError):
                ready = []
            for sock in ready:
                try:
                    data, source = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is self._sock:
                    if source[:2] == (self.client.address or (None,))[:2]:
                        self.client.feed(data)
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
