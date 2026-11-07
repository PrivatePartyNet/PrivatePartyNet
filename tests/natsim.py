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

IPv6 has no NAT: a PC's address is global and keeps its port. What stands in the way is a firewall, of one of these
kinds:

    V6_OPEN            nothing is filtered; anyone may send to the PC
    V6_STATEFUL        only the exact address and port the PC has sent to may answer. Most home routers and ISPs.
    V6_BLOCKS_INBOUND  the PC may send, but no UDP comes in at all, not even answers. Some mobile carriers.
    NO_V6              no IPv6 at all (not a router: give it as v6= to Internet.pc)

Windows gives loopback only one IPv6 address (::1), so IPv6 is carried over IPv4 loopback. Each IPv6 PC gets a global
address in 2001:db8:77::/48 (the documentation prefix), and Internet.pc() returns a transport with the same parts as
pathway.UdpTransport (port, v4, v6, receive, sendto, close). It turns those addresses into the sockets that carry them
and back, so the code under test sees and uses real IPv6 addresses. A router can also have an MTU: a datagram that
would make a bigger packet (28 bytes of IPv4 and UDP header, 48 for IPv6) is dropped, as on a link that does not
fragment.

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
    relay6 = internet.server(relay_sock, family=6)   # the same host on IPv6 too: its global (address, port)
    with home.adopting():                 # UDP sockets bound in this thread during the block are behind `home`
        host = HostPunch(...)
    home.attach(51820)                    # a socket the test cannot reach, by its port on 127.0.0.1
    home.forward(sock)                    # a forwarded port: a public (address, port) anyone may send to
    home.public_address(sock, to)         # the public address `sock` uses towards `to`, or None if it has none yet
    home.dropped                          # what the router refused, as (why, from, to), for assertions and debugging
    home.loss = 0.3                       # bad internet: drops that share of the datagrams through it, both ways
    home.down = True                      # an outage: nothing through it at all until it is False again
    pc = internet.pc(v4=natsim.SYMMETRIC, v6=natsim.V6_STATEFUL)   # transport= for PathManager
    pc.addresses                          # [("ipv6", its global address)], for PathManager's addresses=
    pc.v4_router, pc.v6_router            # their dropped lists say what each refused

Code that makes its own IPv6 socket (HostPunch, join_through) can be put behind an IPv6 firewall too, with
adopting(). Its socket must then be dual-stack (IPV6_V6ONLY off; punch makes it V6ONLY, so a test swaps punch._v6_socket),
and its global address is router.mapped_address, an IPv4-mapped IPv6 address (::ffff:127.77.x.y) that Windows carries
over IPv4 loopback. As on real IPv6 the address keeps the socket's own port, and the firewall's rules apply both ways.

Limits: a packet sent to a public address where no mapping exists reaches a closed port on loopback. Windows then
reports a ConnectionResetError on the sender's socket, where a real router would drop the packet without a word. A
router does not refuse packets between two PCs behind it (hairpinning always works). Mappings never time out unless
`mapping_seconds` is given. A socket attached to an IPv6 router by hand (not through Internet.pc) sees the IPv4
sockets that carry IPv6, not the IPv6 addresses; it can still answer them.
"""

import contextlib
import collections
import ipaddress
import itertools
import random
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

V6_OPEN = "v6 open"
V6_STATEFUL = "v6 stateful"
V6_BLOCKS_INBOUND = "v6 blocks inbound UDP"
NO_V6 = "no v6"
V6_KINDS = (V6_OPEN, V6_STATEFUL, V6_BLOCKS_INBOUND)

PUBLIC_NETWORK = ipaddress.ip_network("127.77.0.0/16")
V6_NETWORK = ipaddress.ip_network("2001:db8:77::/48")
HEADER = {4: 28, 6: 48}  # IP and UDP header bytes a datagram gains on the wire
_addresses = itertools.count(1)  # across every Internet in this process, so a closed one's address is not reused


def _inside(sock_or_port):
    """The (address, port) a PC's socket is seen from on loopback. A socket bound to 0.0.0.0 is seen from 127.0.0.1."""
    if isinstance(sock_or_port, int):
        return ("127.0.0.1", sock_or_port)
    address, port = sock_or_port.getsockname()[:2]
    return ("127.0.0.1" if address in ("0.0.0.0", "", "::") else address, port)


def _name(address):
    """An IPv6 (address, port) in one spelling, so that two ways of writing an address find the same PC."""
    return (str(ipaddress.ip_address(address[0])), int(address[1]))


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
        self.public = sock.getsockname()[:2]  # for IPv6, the IPv4 socket that carries it
        self.name = None  # for IPv6, the global (address, port) others know it by
        self.forwarded = forwarded
        self.sent_to = set()  # public (address, port)s the PC has sent to through this mapping
        self.used = time.monotonic()
        self.dead = False  # timed out; its socket stays open so late packets are dropped, not refused


class Router:
    """One home router (or, for IPv6, one firewall): its public address, its kind, and the PCs behind it. Make one with
    Internet.router()."""

    def __init__(self, internet, kind, address, mapping_seconds=None, mtu=None):
        if kind not in KINDS + V6_KINDS:
            raise ValueError(f"unknown kind of router: {kind!r}")
        self.internet = internet
        self.kind = kind
        self.family = 6 if kind in V6_KINDS else 4
        self.address = address  # the IPv4 address its sockets are on
        self.global_address = str(V6_NETWORK[int(ipaddress.ip_address(address)) & 0xFFFF]) if self.family == 6 else None
        self.mapped_address = f"::ffff:{address}" if self.family == 6 else None  # for adopted IPv6 sockets
        self.mapping_seconds = mapping_seconds
        self.mtu = mtu
        self.dropped = collections.deque(maxlen=1000)
        self.loss = 0.0  # the share of datagrams through this router that are lost on the way (bad internet)
        self.down = False  # an outage: nothing gets through, either way
        self._mappings = {}  # (inside, destination, or None when the mapping is the same for every destination)
        self._forwards = {}  # inside -> its forwarded mapping
        self._pending = []  # weak references to sockets bound in an adopting() block, not yet seen sending

    def __repr__(self):
        return f"<Router {self.kind} {self.global_address or self.address}>"

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
        the public (address, port), for IPv6 its global one."""
        if self.kind in (UDP_BLOCKED, V6_BLOCKS_INBOUND):
            raise ValueError("a network that lets no UDP in cannot forward a UDP port")
        inside = _inside(sock_or_port)
        with self.internet._lock:
            self.internet._attach(self, inside)
            mapping = self._forwards[inside] = self.internet._open(self, inside, forwarded=True)
        return mapping.name or mapping.public

    def public_address(self, sock_or_port, destination=None):
        """The public (address, port) this socket's packets to `destination` come from, or None when it has not sent
        there (for a symmetric router) or anywhere yet."""
        inside = _inside(sock_or_port)
        with self.internet._lock:
            if inside in self._forwards:
                mapping = self._forwards[inside]
            else:
                key = (inside, tuple(destination) if self.kind == SYMMETRIC and destination else None)
                mapping = self._mappings.get(key)
            return (mapping.name or mapping.public) if mapping else None

    def _host(self, inside, port):
        """An IPv6 PC: its one mapping exists from the start, under its global address and its own port."""
        with self.internet._lock:
            self.internet._attach(self, inside)
            self._mappings[(inside, None)] = self.internet._open(self, inside, port=port)

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
        if mapping.forwarded or self.kind in (FULL_CONE, V6_OPEN):
            return True
        if self.kind == V6_BLOCKS_INBOUND:
            self._drop("inbound UDP is blocked", source, mapping.public)
            return False
        if self.kind == RESTRICTED:
            allowed = source[0] in {address for address, _ in mapping.sent_to}
        else:
            allowed = source in mapping.sent_to
        if not allowed:
            self._drop("not an answer to anything sent from there", source, mapping.public)
        return allowed


class PC:
    """A PC with IPv4 behind one router and, if it has IPv6, a firewall of its own: what pathway.PathManager takes as
    transport=. One port for both, as pathway.UdpTransport has. Make one with Internet.pc()."""

    def __init__(self, internet, v4_router, v6_router):
        self.internet = internet
        self.v4_router, self.v6_router = v4_router, v6_router
        self.v4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.v4.bind(("127.0.0.1", 0))
        self.v4.setblocking(False)
        self.port = self.v4.getsockname()[1]
        v4_router.attach(self.v4)
        self.v6 = None  # the IPv4 socket that carries this PC's IPv6
        self.address6 = None
        if v6_router is not None:
            self.v6 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.v6.bind(("127.0.0.1", 0))
            self.v6.setblocking(False)
            v6_router._host(_inside(self.v6), self.port)
            self.address6 = v6_router.global_address

    @property
    def addresses(self):
        """What this PC offers about itself besides STUN: its IPv6 address, if it has one."""
        return [("ipv6", self.address6)] if self.address6 else []

    def receive(self, timeout):
        """(data, (address, port)) or None after `timeout` seconds. IPv6 comes from its global address."""
        sockets = [s for s in (self.v4, self.v6) if s is not None]
        try:
            ready, _, _ = select.select(sockets, [], [], timeout)
        except (OSError, ValueError):
            return None
        for sock in ready:
            try:
                data, source = sock.recvfrom(65535)
            except OSError:
                continue
            if sock is self.v6:
                source = self.internet._name_of(source[:2])
                if source is None:
                    continue  # not from anyone on the IPv6 internet
            return data, tuple(source[:2])
        return None

    def sendto(self, data, address):
        if ":" in address[0]:
            if self.v6 is None:
                return False
            carrier = self.internet._carrier_of(address)
            if carrier is None:
                return True  # nobody has that address: lost on the way, as on the internet
            sock, to = self.v6, carrier
        else:
            sock, to = self.v4, tuple(address[:2])
        try:
            sock.sendto(data, to)
            return True
        except OSError:
            return False

    def close(self):
        for sock in (self.v4, self.v6):
            if sock is not None:
                sock.close()


class Internet:
    """The pretend internet: every router on it, and one thread that carries packets between them."""

    def __init__(self):
        self._lock = threading.RLock()
        self._routers = []
        self._inside = {}  # a PC's (address, port) on loopback -> {4 or 6: its Router}
        self._by_socket = {}  # public socket -> its _Mapping
        self._by_public = {}  # an IPv6 mapping's carrying (address, port) -> its _Mapping
        self._by_name = {}  # a global IPv6 (address, port) -> its _Mapping
        self._servers = []  # fake STUN servers to close
        self._pcs = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="natsim", daemon=True)
        self._thread.start()

    def router(self, kind=PORT_RESTRICTED, mapping_seconds=None, mtu=None):
        """A new router (or IPv6 firewall) with its own public address."""
        if kind == NO_V6:
            raise ValueError("no IPv6 is not a firewall; give v6=NO_V6 to Internet.pc")
        address = str(PUBLIC_NETWORK[next(_addresses)])
        router = Router(self, kind, address, mapping_seconds, mtu)
        with self._lock:
            self._routers.append(router)
        return router

    def server(self, sock_or_port, family=4):
        """A public host with no NAT or firewall in front of it (a STUN server, a relay on a VPS). Returns its public
        address, or with family=6 its global IPv6 address. Call it once for each family to have both."""
        return self.router(FULL_CONE if family == 4 else V6_OPEN).forward(sock_or_port)

    def pc(self, v4=PORT_RESTRICTED, v6=NO_V6, v4_mtu=None, v6_mtu=None):
        """A PC with IPv4 behind a `v4` router and IPv6 behind a `v6` firewall (or none), as a transport for
        pathway.PathManager. Closed with the internet."""
        v4_router = self.router(v4, mtu=v4_mtu)
        v6_router = None if v6 in (None, NO_V6) else self.router(v6, mtu=v6_mtu)
        if v6_router is not None and v6_router.family != 6:
            raise ValueError(f"not a kind of IPv6 firewall: {v6!r}")
        made = PC(self, v4_router, v6_router)
        self._pcs.append(made)
        return made

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
        for made in self._pcs:
            made.close()
        with self._lock:
            for sock in self._by_socket:
                sock.close()
            self._by_socket.clear()

    def _attach(self, router, inside):
        with self._lock:
            self._inside.setdefault(inside, {})[router.family] = router

    def _open(self, router, inside, forwarded=False, port=None, same_port=False):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((router.address, inside[1] if same_port else 0))
        sock.setblocking(False)
        mapping = _Mapping(router, inside, sock, forwarded)
        self._by_socket[sock] = mapping
        if same_port:
            mapping.name = (router.mapped_address, inside[1])  # an adopted IPv6 socket: its own port, mapped address
        elif router.family == 6:
            mapping.name = (router.global_address, port or inside[1])  # no NAT: the PC's own port
            self._by_public[mapping.public] = mapping
            self._by_name[mapping.name] = mapping
        return mapping

    def _name_of(self, carrier):
        with self._lock:
            mapping = self._by_public.get(tuple(carrier))
            return mapping.name if mapping else None

    def _carrier_of(self, address):
        try:
            name = _name(address)
        except ValueError:
            return None
        with self._lock:
            mapping = self._by_name.get(name)
            return mapping.public if mapping else None

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
                self._inside.setdefault(inside, {})[router.family] = router
                if router.family == 6 and (inside, None) not in router._mappings:
                    # IPv6 has no NAT: the socket is reachable at its own port from the start, as far as the firewall
                    # lets anything in.
                    router._mappings[(inside, None)] = self._open(router, inside, same_port=True)
            else:
                keep.append(reference)
        router._pending = keep

    def _sender(self, source, family):
        routers = self._inside.get(source)
        if routers is None:
            for candidate in self._routers:
                if candidate._pending:
                    self._settle(candidate)
            routers = self._inside.get(source)
        return (routers or {}).get(family)

    def _carry(self, mapping, data, source):
        """A packet from `source` (a PC on loopback) arrived at `mapping`'s public socket."""
        with self._lock:
            receiver = mapping.router
            sender = self._sender(source, receiver.family)
            if sender is None:
                receiver._drop(f"from a socket on no IPv{receiver.family} router (attach it, or make it a server)",
                               source, mapping.public)
                return
            if sender.down or receiver.down:
                (sender if sender.down else receiver)._drop("the link is down", source, mapping.public)
                return
            loss = max(sender.loss, receiver.loss)
            if loss and random.random() < loss:
                return  # lost on the way
            mtu = min((m for m in (sender.mtu, receiver.mtu) if m), default=None)
            if mtu is not None and len(data) + HEADER[receiver.family] > mtu:
                sender._drop(f"a {len(data)}-byte datagram is bigger than the path's MTU of {mtu}", source,
                             mapping.public)
                return
            out = sender._out(source, mapping.public)
            if out is None or not receiver._lets_in(mapping, out.public):
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
