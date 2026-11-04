"""A home router in software, for the path tests: a stand-in for pathway.UdpTransport that sits behind a NAT.

Each outside "mapping" is a real UDP socket on loopback, so other PCs in the test see an ordinary public address and
port. A mapping lets in only datagrams from addresses this PC has sent to through it (a port-restricted router, the
usual kind). An easy router keeps one mapping for every destination, so the address STUN sees is the one others can
punch to. A hard router ("symmetric", as carrier-grade NAT often is) makes a new mapping for every destination, so the
address STUN sees is useless to anyone else, and two hard routers can never reach each other without a relay.
"""

import select
import socket
import threading
import time


class NatTransport:
    def __init__(self, symmetric):
        self.symmetric = symmetric
        self.port = 40000  # the port on the PC's own side of the router; nobody outside can use it
        self.v6 = None
        self._mappings = {}  # destination (or None for an easy router) -> socket
        self._allowed = {}  # socket -> destinations it has sent to
        self._lock = threading.Lock()
        self.dropped = 0
        self.closed = False

    def _mapping(self, destination):
        key = destination if self.symmetric else None
        with self._lock:
            sock = self._mappings.get(key)
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("127.0.0.1", 0))
                sock.setblocking(False)
                self._mappings[key] = sock
                self._allowed[sock] = set()
            self._allowed[sock].add(tuple(destination))
            return sock

    def public_ports(self):
        with self._lock:
            return sorted(s.getsockname()[1] for s in self._mappings.values())

    def sendto(self, data, address):
        if self.closed:
            return False
        try:
            self._mapping(tuple(address[:2])).sendto(data, address[:2])
            return True
        except OSError:
            return False

    def receive(self, timeout):
        ends = time.monotonic() + timeout
        while not self.closed:
            with self._lock:
                sockets = list(self._mappings.values())
            left = ends - time.monotonic()
            if left <= 0:
                return None
            if not sockets:
                time.sleep(min(left, 0.05))
                continue
            try:
                ready, _, _ = select.select(sockets, [], [], min(left, 0.05))
            except (OSError, ValueError):
                return None
            for sock in ready:
                try:
                    data, source = sock.recvfrom(65535)
                except OSError:
                    continue
                if tuple(source[:2]) in self._allowed.get(sock, ()):
                    return data, tuple(source[:2])
                self.dropped += 1  # the router has no reason to let this in
        return None

    def close(self):
        self.closed = True
        with self._lock:
            for sock in self._mappings.values():
                sock.close()
            self._mappings.clear()
