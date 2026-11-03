"""A pretend internet with home routers on it, all on this PC, for testing connections through NAT.

Each router has its own public address in 127.77.0.0/16 and keeps a real UDP socket there for each mapping it makes.
That means the code under test uses ordinary sockets (asyncio, aioquic, a WireGuard tunnel) and needs no changes. A PC
"behind" a router is a UDP socket on 127.0.0.1 that the router has been told about. When it sends to a public address,
the packet lands on that address's socket. The simulator then does two things. It applies the sender's router on the
way out (pick or make a mapping, and remember where the packet went). It applies the receiver's router on the way in
(let the packet in or drop it). A packet that gets in is handed over from the sender's public address, so the receiver
sees that address as the source. Packets to anywhere else, such as a fake DHT on 127.0.0.1, are left alone.

The kinds of router (RFC 4787 names in brackets):

    FULL_CONE        one public port per socket; anyone may send to it once it exists (endpoint-independent filtering)
    RESTRICTED       one public port per socket; only addresses the PC has sent to may answer, from any port
                     (address-dependent filtering)
    PORT_RESTRICTED  one public port per socket; only the exact address and port the PC has sent to may answer
                     (address-and-port-dependent filtering). Most home routers.
    SYMMETRIC        a new public port for every destination, and only that destination may answer
                     (address-and-port-dependent mapping). Many carrier-grade NATs and mobile networks.
    UDP_BLOCKED      no UDP in or out at all

TCP is not translated. A test that wants "only TCP gets through" uses UDP_BLOCKED and opens TCP connections only to a
server it treats as public (a relay). No router lets an unasked-for TCP connection in, so a test should not dial a PC
that is behind a router.

Use:

    internet = natsim.Internet()
    self.addCleanup(internet.close)
    home = internet.router(natsim.SYMMETRIC)
    sock = home.socket()                  # a UDP socket on 127.0.0.1, behind that router
    stun = internet.stun_servers(2)       # pass as stun_servers=; each reports the public address it sees
    relay = internet.server(relay_sock)   # a public host with no NAT; returns its public (address, port)
    with home.adopting():                 # UDP sockets bound in this thread during the block are behind `home`
        host = HostPunch(...)
    home.attach(51820)                    # a socket the test cannot reach, by its port on 127.0.0.1
    home.forward(sock)                    # a forwarded port: a public (address, port) anyone may send to
    home.public_address(sock, to)         # the public address `sock` uses towards `to`, or None if it has none yet
    home.dropped                          # what the router refused, as (why, from, to), for assertions and debugging

Limits: a packet sent to a public address where no mapping exists reaches a closed port on loopback. Windows then
reports a ConnectionResetError on the sender's socket, where a real router would drop the packet without a word. A
router does not refuse packets between two PCs behind it (hairpinning always works). Mappings never time out unless
`mapping_seconds` is given.
"""

import contextlib
import collections
import ipaddress
import itertools
import select
import socket
import sys
import threading
import time
import weakref

FULL_CONE = "full cone"
RESTRICTED = "restricted cone"
PORT_RESTRICTED = "port-restricted cone"
SYMMETRIC = "symmetric"
UDP_BLOCKED = "udp blocked"
KINDS = (FULL_CONE, RESTRICTED, PORT_RESTRICTED, SYMMETRIC, UDP_BLOCKED)

PUBLIC_NETWORK = ipaddress.ip_network("127.77.0.0/16")
_addresses = itertools.count(1)  # across every Internet in this process, so a closed one's address is not reused


def _inside(sock_or_port):
    """The (address, port) a PC's socket is seen from on loopback. A socket bound to 0.0.0.0 is seen from 127.0.0.1."""
    if isinstance(sock_or_port, int):
        return ("127.0.0.1", sock_or_port)
    address, port = sock_or_port.getsockname()[:2]
    return ("127.0.0.1" if address in ("0.0.0.0", "") else address, port)


# --- picking up sockets that other code makes ------------------------------------------------------------------------

_adopters = []  # (router, thread or None for any thread) while a Router.adopting() block is open
_hooked = False


def _audit(event, args):
    if event != "socket.bind" or not _adopters:
        return
    try:
        sock = args[0]
        if sock.type != socket.SOCK_DGRAM:
            return
        for router, thread in list(_adopters):
            if thread is None or thread is threading.current_thread():
                router._pending.append(weakref.ref(sock))
    except Exception:
        pass  # an audit hook must never break the code it watches


def _hook():
    # Audit hooks cannot be removed, so one is added for the whole process and does nothing outside adopting().
    global _hooked
    if not _hooked:
        sys.addaudithook(_audit)
        _hooked = True


# --- mappings and routers --------------------------------------------------------------------------------------------


class _Mapping:
    def __init__(self, router, inside, sock, forwarded=False):
        self.router = router
        self.inside = inside
        self.sock = sock
        self.public = sock.getsockname()[:2]
        self.forwarded = forwarded
        self.sent_to = set()  # public (address, port)s the PC has sent to through this mapping
        self.used = time.monotonic()
        self.dead = False  # timed out; its socket stays open so late packets are dropped, not refused


class Router:
    """One home router: its public address, its kind, and the PCs behind it. Make one with Internet.router()."""

    def __init__(self, internet, kind, address, mapping_seconds=None):
        if kind not in KINDS:
            raise ValueError(f"unknown kind of router: {kind!r}")
        self.internet = internet
        self.kind = kind
        self.address = address
        self.mapping_seconds = mapping_seconds
        self.dropped = collections.deque(maxlen=1000)
        self._mappings = {}  # (inside, destination, or None when the mapping is the same for every destination)
        self._forwards = {}  # inside -> its forwarded mapping
        self._pending = []  # weak references to sockets bound in an adopting() block, not yet seen sending

    def __repr__(self):
        return f"<Router {self.kind} {self.address}>"

    def attach(self, sock_or_port):
        """Put a PC's UDP socket (or its port on 127.0.0.1) behind this router."""
        self.internet._attach(self, _inside(sock_or_port))
        return sock_or_port

    def socket(self):
        """A new UDP socket on 127.0.0.1, already behind this router."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        return self.attach(sock)

    @contextlib.contextmanager
    def adopting(self, any_thread=False):
        """While the block runs, UDP sockets bound in this thread (or in any thread) are put behind this router. For
        code that makes its own socket, such as HostPunch or join_through."""
        _hook()
        entry = (self, None if any_thread else threading.current_thread())
        _adopters.append(entry)
        try:
            yield self
        finally:
            _adopters.remove(entry)
            with self.internet._lock:
                self.internet._settle(self)

    def forward(self, sock_or_port):
        """Forward a public port to this socket: anyone may send to it, and its own packets go out from it. Returns
        the public (address, port)."""
        if self.kind == UDP_BLOCKED:
            raise ValueError("a router that blocks UDP cannot forward a UDP port")
        inside = _inside(sock_or_port)
        with self.internet._lock:
            self.internet._attach(self, inside)
            mapping = self._forwards[inside] = self.internet._open(self, inside, forwarded=True)
        return mapping.public

    def public_address(self, sock_or_port, destination=None):
        """The public (address, port) this socket's packets to `destination` come from, or None when it has not sent
        there (for a symmetric router) or anywhere yet."""
        inside = _inside(sock_or_port)
        with self.internet._lock:
            if inside in self._forwards:
                return self._forwards[inside].public
            key = (inside, tuple(destination) if self.kind == SYMMETRIC and destination else None)
            mapping = self._mappings.get(key)
            return mapping.public if mapping else None

    def _drop(self, why, source, destination):
        self.dropped.append((why, source, destination))

    def _timed_out(self, mapping, now):
        return mapping.dead or (not mapping.forwarded and self.mapping_seconds is not None and
                                now - mapping.used > self.mapping_seconds)

    def _out(self, inside, destination):
        """The mapping a packet from `inside` to the public `destination` leaves by, made if needed; None if dropped."""
        if self.kind == UDP_BLOCKED:
            self._drop("UDP is blocked", inside, destination)
            return None
        now = time.monotonic()
        mapping = self._forwards.get(inside)
        if mapping is None:
            key = (inside, destination if self.kind == SYMMETRIC else None)
            mapping = self._mappings.get(key)
            if mapping is not None and self._timed_out(mapping, now):
                mapping.dead = True
                mapping = None
            if mapping is None:
                mapping = self._mappings[key] = self.internet._open(self, inside)
        mapping.sent_to.add(destination)
        mapping.used = now
        return mapping

    def _lets_in(self, mapping, source):
        """Whether a packet from the public `source` may come in through `mapping`."""
        if self._timed_out(mapping, time.monotonic()):
            self._drop("the mapping timed out", source, mapping.public)
            return False
        if mapping.forwarded or self.kind == FULL_CONE:
            return True
        if self.kind == RESTRICTED:
            allowed = source[0] in {address for address, _ in mapping.sent_to}
        else:
            allowed = source in mapping.sent_to
        if not allowed:
            self._drop("not an answer to anything sent from there", source, mapping.public)
        return allowed


class Internet:
    """The pretend internet: every router on it, and one thread that carries packets between them."""

    def __init__(self):
        self._lock = threading.RLock()
        self._routers = []
        self._inside = {}  # a PC's (address, port) on loopback -> its Router
        self._by_socket = {}  # public socket -> its _Mapping
        self._servers = []  # fake STUN servers to close
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="natsim", daemon=True)
        self._thread.start()

    def router(self, kind=PORT_RESTRICTED, mapping_seconds=None):
        """A new router with its own public address."""
        address = str(PUBLIC_NETWORK[next(_addresses)])
        router = Router(self, kind, address, mapping_seconds)
        with self._lock:
            self._routers.append(router)
        return router

    def server(self, sock_or_port):
        """A public host with no NAT in front of it (a STUN server, a relay on a VPS). Returns its public address."""
        return self.router(FULL_CONE).forward(sock_or_port)

    def stun_servers(self, count=2):
        """`count` fake STUN servers on the internet, as a tuple of public addresses for stun_servers=. Each reports the
        public address it sees, so a symmetric router shows a new port to each one."""
        from test_connect import FakeStun

        found = []
        for _ in range(count):
            fake = FakeStun()
            self._servers.append(fake)
            found.append(self.server(fake.socket))
        return tuple(found)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        for fake in self._servers:
            fake.close()
        with self._lock:
            for sock in self._by_socket:
                sock.close()
            self._by_socket.clear()

    def _attach(self, router, inside):
        with self._lock:
            self._inside[inside] = router

    def _open(self, router, inside, forwarded=False):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((router.address, 0))
        sock.setblocking(False)
        mapping = _Mapping(router, inside, sock, forwarded)
        self._by_socket[sock] = mapping
        return mapping

    def _settle(self, router):
        """Attach the router's adopted sockets that have a port by now; forget the ones that are closed."""
        keep = []
        for reference in router._pending:
            sock = reference()
            if sock is None or sock.fileno() == -1:
                continue
            try:
                inside = _inside(sock)
            except OSError:
                continue
            if inside[1]:
                self._inside[inside] = router
            else:
                keep.append(reference)
        router._pending = keep

    def _sender(self, source):
        router = self._inside.get(source)
        if router is None:
            for candidate in self._routers:
                if candidate._pending:
                    self._settle(candidate)
            router = self._inside.get(source)
        return router

    def _carry(self, mapping, data, source):
        """A packet from `source` (a PC on loopback) arrived at `mapping`'s public socket."""
        with self._lock:
            sender = self._sender(source)
            if sender is None:
                mapping.router._drop("from a socket on no router (attach it, or make it a server)", source,
                                     mapping.public)
                return
            out = sender._out(source, mapping.public)
            if out is None or not mapping.router._lets_in(mapping, out.public):
                return
            via, to = out.sock, mapping.inside
        try:
            via.sendto(data, to)
        except OSError:
            pass  # the PC's socket is gone

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                sockets = list(self._by_socket)
            if not sockets:
                self._stop.wait(0.02)
                continue
            try:
                ready, _, _ = select.select(sockets, [], [], 0.05)
            except (OSError, ValueError):
                continue  # a socket was closed under us; the next round has a fresh list
            for sock in ready:
                try:
                    data, source = sock.recvfrom(65535)
                except OSError:
                    continue  # on Windows, the reply to an earlier packet to a closed port
                mapping = self._by_socket.get(sock)
                if mapping is not None:
                    self._carry(mapping, data, source[:2])
