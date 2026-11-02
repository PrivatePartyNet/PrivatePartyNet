"""The game network: each server is also a small private network, so games that play over a LAN work between members.

How it fits together:

* Each server has its own /24 network in 10.0.0.0/8, chosen from the server id and moved off any network this PC is
  already on. The host is .1; each member keeps the same address on that server every time (remembered by their id).
* Packets travel over the party's own TLS connection to the host (route /net), which a member opens with the same signed
  proof of who they are as the join. The host routes them: to one member by address, or to everyone for a broadcast or
  multicast (how games find each other on a LAN). A packet whose sender address is not the sender's own is dropped, so
  nobody can pretend to be someone else on the game network.
* On each PC a virtual network adapter (Wintun) gives Windows and the games an ordinary network card with that address.
  Making one needs administrator rights (measured: error 5 as a normal user), so a small helper started with Windows'
  permission prompt holds the adapter and passes packets to the app over a loopback connection; see nethelper.py.
* Only IPv4 to this server's network (and broadcast and multicast) is carried. Anything else Windows sends on the adapter
  (IPv6, traffic for the internet) is dropped, so the game network never becomes a route to anywhere else.

Frames on every link: two bytes of length (big-endian), then one IPv4 packet. A length of zero is a keep-alive.
"""

import hashlib
import ipaddress
import queue
import socket
import struct
import threading
import time

FRAME_HEADER = struct.Struct(">H")
MAX_PACKET = 65535
QUEUE_PACKETS = 1024  # per member; a member who cannot keep up loses packets instead of slowing the others
KEEPALIVE_SECONDS = 15.0
AVOID_SECOND_OCTET = {0, 1, 2, 5, 8, 10, 20, 50, 100, 168, 200, 254, 255}  # 10.x networks routers and VPNs often use
MULTICAST = ipaddress.ip_network("224.0.0.0/4")
LIMITED_BROADCAST = ipaddress.ip_address("255.255.255.255")


class NetError(Exception):
    pass


# --- addresses --------------------------------------------------------------------------------------------------


def subnet_for(server_id, in_use=()):
    """The /24 a server's game network uses: 10.a.b.0/24 from a hash of the server id, skipping second octets that are
    common on home networks and any network in `in_use` (this PC's own networks). Same server id, same network."""
    taken = [ipaddress.ip_network(n, strict=False) for n in in_use]
    for attempt in range(256):
        digest = hashlib.sha256(f"PrivatePartyNet game network|{server_id}|{attempt}".encode("utf-8")).digest()
        a, b = 16 + digest[0] % 224, digest[1]
        if a in AVOID_SECOND_OCTET or b in (0, 255):
            continue
        network = ipaddress.ip_network(f"10.{a}.{b}.0/24")
        if not any(network.overlaps(other) for other in taken):
            return network
    raise NetError("no free network could be found for this server")


def local_networks():
    """The IPv4 networks this PC is on, each at its real size (NordVPN's NordLynx, for one, is a /16: 10.5.0.0/16 on
    the author's PC). Read from the adapters with psutil; falls back to treating each address as a /24."""
    found = set()
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for entry in addresses:
                if entry.family == socket.AF_INET and entry.address and entry.netmask:
                    try:
                        found.add(ipaddress.ip_network(f"{entry.address}/{entry.netmask}", strict=False))
                    except ValueError:
                        continue
    except Exception:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.add(ipaddress.ip_network(f"{info[4][0]}/24", strict=False))
        except OSError:
            return []
    return sorted(found)


# --- packets ------------------------------------------------------------------------------------------------------


def parse(packet):
    """(source, destination) of an IPv4 packet, or None for anything that is not a well-formed IPv4 packet."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    header = (packet[0] & 0x0F) * 4
    total = struct.unpack(">H", packet[2:4])[0]
    if header < 20 or len(packet) < header or total < header or total > len(packet):
        return None
    return ipaddress.IPv4Address(packet[12:16]), ipaddress.IPv4Address(packet[16:20])


def is_group(destination, network):
    """True for a packet to everyone: this network's broadcast address, 255.255.255.255, or multicast."""
    return destination == network.broadcast_address or destination == LIMITED_BROADCAST or destination in MULTICAST


def carried(packet, network):
    """True when a packet belongs on the game network: IPv4, to this network or to everyone. Everything else Windows
    puts on the adapter is dropped before it leaves the PC."""
    parsed = parse(packet)
    if parsed is None:
        return False
    _, destination = parsed
    return destination in network or is_group(destination, network)


# --- framing ------------------------------------------------------------------------------------------------------


def write_frame(stream, packet):
    if len(packet) > MAX_PACKET:
        raise NetError("packet too large")
    stream.write(FRAME_HEADER.pack(len(packet)) + packet)
    stream.flush()


def read_exact(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError("the link closed")
        data += chunk
    return data


def read_frame(stream):
    """The next packet, or b"" for a keep-alive. Raises EOFError when the link closes."""
    (length,) = FRAME_HEADER.unpack(read_exact(stream, FRAME_HEADER.size))
    return read_exact(stream, length) if length else b""


# --- the host's router --------------------------------------------------------------------------------------------


class Router:
    """Routes packets between the members of one server's game network. Thread safe.

    `assignments` maps a member's permanent id to the last byte of their address, and is kept by the caller (the party
    saves it with the server), so a member has the same address every time."""

    def __init__(self, network, assignments=None, on_change=None):
        self.network = ipaddress.ip_network(network)
        self._assign = dict(assignments or {})
        self._on_change = on_change or (lambda assignments: None)
        self._links = {}  # address -> queue of packets for that member
        self._lock = threading.Lock()
        self.dropped = 0

    @property
    def host_address(self):
        return self.network.network_address + 1

    def address_for(self, user):
        """This member's address on the network, given the first time and kept. The host (user None) is .1."""
        if user is None:
            return self.host_address
        with self._lock:
            if user not in self._assign:
                used = set(self._assign.values())
                free = next((n for n in range(2, 255) if n not in used), None)
                if free is None:
                    raise NetError("this game network is full")
                self._assign[user] = free
                changed = dict(self._assign)
            else:
                changed = None
            last = self._assign[user]
        if changed is not None:
            self._on_change(changed)
        return self.network.network_address + last

    def assignments(self):
        with self._lock:
            return dict(self._assign)

    def attach(self, address):
        """Start carrying packets for `address`. Returns the queue its packets arrive on. A second attach for the same
        address replaces the first (the member reconnected)."""
        inbox = queue.Queue(maxsize=QUEUE_PACKETS)
        with self._lock:
            self._links[ipaddress.IPv4Address(address)] = inbox
        return inbox

    def detach(self, address, inbox=None):
        with self._lock:
            address = ipaddress.IPv4Address(address)
            if inbox is None or self._links.get(address) is inbox:
                self._links.pop(address, None)

    def online(self):
        with self._lock:
            return sorted(str(a) for a in self._links)

    def route(self, sender, packet):
        """Deliver one packet from the member at `sender`. Returns how many members it went to."""
        parsed = parse(packet)
        sender = ipaddress.IPv4Address(sender)
        if parsed is None:
            self.dropped += 1
            return 0
        source, destination = parsed
        if source != sender:
            self.dropped += 1  # someone writing another member's address as the sender
            return 0
        with self._lock:
            if is_group(destination, self.network):
                targets = [inbox for address, inbox in self._links.items() if address != sender]
            elif destination in self.network and destination != sender:
                targets = [self._links[destination]] if destination in self._links else []
            else:
                targets = []
        delivered = 0
        for inbox in targets:
            try:
                inbox.put_nowait(packet)
                delivered += 1
            except queue.Full:
                self.dropped += 1
        return delivered


def pump_link(rfile, wfile, router, address, inbox, stop, keepalive=KEEPALIVE_SECONDS):
    """Carry packets both ways between one member's link and the router until the link closes or `stop` is set.
    Used by the party for each member on /net. Returns when done; never raises for a closed link."""
    done = threading.Event()

    def reader():
        try:
            while not stop.is_set() and not done.is_set():
                packet = read_frame(rfile)
                if packet:
                    router.route(address, packet)
        except (EOFError, OSError, struct.error, ValueError):
            pass
        finally:
            done.set()

    thread = threading.Thread(target=reader, name="privatepartynet-net-read", daemon=True)
    thread.start()
    last = time.monotonic()
    try:
        while not stop.is_set() and not done.is_set():
            try:
                packet = inbox.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - last >= keepalive:
                    write_frame(wfile, b"")
                    last = time.monotonic()
                continue
            write_frame(wfile, packet)
            last = time.monotonic()
    except (OSError, ValueError):
        pass
    finally:
        done.set()
        router.detach(address, inbox)
        thread.join(timeout=2)


# --- a stand-in adapter for tests -----------------------------------------------------------------------------------


class MemoryTun:
    """A virtual adapter in memory with the same two calls the real one offers: read() for a packet Windows wants to
    send (None after a timeout), write(packet) for one that arrived. Tests feed it and read from it."""

    def __init__(self):
        self.outgoing = queue.Queue()  # what "Windows" sends: read() returns these
        self.incoming = queue.Queue()  # what was delivered to "Windows"

    def read(self, timeout=0.5):
        try:
            return self.outgoing.get(timeout=timeout)
        except queue.Empty:
            return None

    def write(self, packet):
        self.incoming.put(packet)

    def close(self):
        pass


def make_packet(source, destination, payload=b"", protocol=17):
    """An IPv4 packet (UDP by default) with a correct header checksum, for tests and keep-alive probes."""
    total = 20 + len(payload)
    header = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, protocol, 0,
                         ipaddress.IPv4Address(source).packed, ipaddress.IPv4Address(destination).packed)
    checksum = 0
    for index in range(0, 20, 2):
        checksum += (header[index] << 8) + header[index + 1]
    while checksum >> 16:
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
    header = header[:10] + struct.pack(">H", ~checksum & 0xFFFF) + header[12:]
    return header + payload


def bridge(tun, send, network, stop):
    """Move packets from a virtual adapter to `send(packet)` until `stop` is set: only what belongs on the game network.
    Runs on the caller's thread."""
    while not stop.is_set():
        packet = tun.read(timeout=0.5)
        if packet and carried(packet, network):
            send(packet)
