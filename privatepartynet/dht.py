"""A small client for the BitTorrent Mainline DHT, enough to publish and read signed records (BEP 44).

This is how two PrivatePartyNet PCs that cannot reach each other yet swap the public addresses their routers gave them, with no
server of ours: each writes a small record signed with an Ed25519 key into the DHT, a network of millions of BitTorrent
nodes, and the other reads it. Nothing in a record is secret (an address and a time), and each one is signed, so a node
cannot change it. See SPEC Part 9.

Only outbound UDP is used, which carrier-grade NAT allows. Every wait is bounded: one query waits at most `timeout`
seconds, and a whole lookup stops after `deadline` seconds or `max_queries` queries, whichever comes first.
"""

import hashlib
import ipaddress
import os
import socket
import struct
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

BOOTSTRAP = (("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881), ("router.utorrent.com", 6881))
K = 8  # nodes kept per lookup (Kademlia's k)
ALPHA = 8  # queries in flight at once
MAX_VALUE = 1000
MAX_SALT = 64


class DhtError(Exception):
    pass


# Nodes that answered recently, shared by every client in this process, so a lookup does not depend on the three
# bootstrap routers answering (measured: when they drop a packet, a lookup that only had them finds nothing).
_known = {}  # (ip, port) -> node id
_known_lock = threading.Lock()
MAX_KNOWN = 256


def _remember(address, node):
    with _known_lock:
        _known[address] = node
        while len(_known) > MAX_KNOWN:
            _known.pop(next(iter(_known)))


# --- bencode ------------------------------------------------------------------------------------------------------


def bencode(value):
    if isinstance(value, bool):
        raise TypeError("bencode has no booleans")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return b"%d:" % len(value) + bytes(value)
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(((k.encode("utf-8") if isinstance(k, str) else bytes(k)), v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")


def bdecode(data):
    """Decode one bencoded value. Keys come back as bytes. Raises DhtError for anything malformed or with trailing data."""
    value, end = _decode(memoryview(bytes(data)), 0, 0)
    if end != len(data):
        raise DhtError("trailing data")
    return value


def _decode(data, at, depth):
    if depth > 32 or at >= len(data):
        raise DhtError("malformed")
    head = data[at:at + 1].tobytes()
    if head == b"i":
        end = bytes(data[at:]).index(b"e") + at
        text = data[at + 1:end].tobytes()
        if not text or (text.startswith(b"0") and text != b"0") or text.startswith(b"-0"):
            raise DhtError("malformed integer")
        return int(text), end + 1
    if head == b"l":
        at, items = at + 1, []
        while data[at:at + 1].tobytes() != b"e":
            item, at = _decode(data, at, depth + 1)
            items.append(item)
        return items, at + 1
    if head == b"d":
        at, items = at + 1, {}
        while data[at:at + 1].tobytes() != b"e":
            key, at = _decode(data, at, depth + 1)
            if not isinstance(key, bytes):
                raise DhtError("malformed key")
            items[key], at = _decode(data, at, depth + 1)
        return items, at + 1
    if head.isdigit():
        colon = bytes(data[at:at + 12]).find(b":")
        if colon < 0:
            raise DhtError("malformed string")
        length = int(data[at:at + colon].tobytes())
        start = at + colon + 1
        if start + length > len(data):
            raise DhtError("string past the end")
        return data[start:start + length].tobytes(), start + length
    raise DhtError("malformed")


# --- BEP 44 -------------------------------------------------------------------------------------------------------


def target_for(public_key, salt=b""):
    return hashlib.sha1(bytes(public_key) + bytes(salt)).digest()


def signed_bytes(seq, value, salt=b""):
    """What the owner signs for a mutable item: [4:salt<n>:<salt>]3:seqi<seq>e1:v<bencoded value>."""
    prefix = b"4:salt%d:%s" % (len(salt), salt) if salt else b""
    return prefix + b"3:seqi%de1:v" % seq + bencode(value)


def check_item(public_key, salt, seq, value, signature):
    try:
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(bytes(signature), signed_bytes(seq, value, salt))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- node addresses -------------------------------------------------------------------------------------------------


def compact_nodes(data):
    """The (node id, (ip, port)) pairs in a compact "nodes" string (26 bytes each)."""
    out = []
    for at in range(0, len(data) - len(data) % 26, 26):
        node, ip, port = data[at:at + 20], data[at + 20:at + 24], struct.unpack(">H", data[at + 24:at + 26])[0]
        address = str(ipaddress.IPv4Address(ip))
        if port and not ipaddress.IPv4Address(ip).is_unspecified:
            out.append((node, (address, port)))
    return out


def distance(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


# --- the client -----------------------------------------------------------------------------------------------------


class Dht:
    """A DHT client on one UDP socket. Not a full node: it asks, and answers nobody (other nodes do not need it to)."""

    def __init__(self, sock=None, bootstrap=BOOTSTRAP, timeout=1.0, deadline=20.0, max_queries=300):
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sock is None:
            self._sock.bind(("0.0.0.0", 0))
        self._sock.settimeout(0.2)
        self.node_id = os.urandom(20)
        self._bootstrap = bootstrap
        self._timeout, self._deadline, self._max_queries = timeout, deadline, max_queries
        self._pending = {}  # transaction id -> [event, reply]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._receive, name="privatepartynet-dht", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    # one query

    def _receive(self):
        while not self._stop.is_set():
            try:
                data, source = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            try:
                message = bdecode(data)
            except (DhtError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            with self._lock:
                waiting = self._pending.get(message.get(b"t"))
            if waiting is not None:
                waiting[1] = message
                waiting[0].set()

    def query(self, address, method, arguments, timeout=None):
        """Send one query and wait for its answer: the reply dict ("r"), or None for no answer or an error."""
        transaction = os.urandom(4)
        event = threading.Event()
        with self._lock:
            self._pending[transaction] = [event, None]
        try:
            packet = bencode({"t": transaction, "y": "q", "q": method, "a": {"id": self.node_id, **arguments}})
            try:
                self._sock.sendto(packet, address)
            except OSError:
                return None
            if not event.wait(self._timeout if timeout is None else timeout):
                return None
            with self._lock:
                reply = self._pending[transaction][1]
            if reply.get(b"y") != b"r" or not isinstance(reply.get(b"r"), dict):
                return None
            return reply[b"r"]
        finally:
            with self._lock:
                self._pending.pop(transaction, None)

    # lookups

    def _seeds(self):
        found = []
        for host, port in self._bootstrap:
            try:
                for info in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM):
                    if info[4][:2] not in found:
                        found.append(info[4][:2])
            except OSError:
                continue
        return found

    def lookup(self, target, method="get", extra=None):
        """Walk towards `target`, asking the closest nodes found so far. Returns [(node id, address, reply)] for the
        closest nodes that answered, nearest first. With method "get" the replies carry tokens and any stored item."""
        extra = extra or {}
        started = time.monotonic()
        candidates = {}  # address -> node id (None for a bootstrap node, whose id is not known yet)
        with _known_lock:
            candidates.update(_known)
        seeds = self._seeds()
        for address in seeds:
            candidates.setdefault(address, None)
        bootstrap_tries = 1
        asked, answered = set(), []
        lock = threading.Lock()
        queries = 0

        def ask(address):
            # Bootstrap routers (no id known yet) answer find_node only; real nodes answer get as well (measured).
            bootstrap = candidates.get(address) is None
            reply = self.query(address, "find_node" if bootstrap else method,
                               {"target": target} if bootstrap else {"target": target, **extra},
                               timeout=max(2.5, self._timeout) if bootstrap else None)  # routers answer slower
            if reply is None:
                return
            node = reply.get(b"id")
            with lock:
                if isinstance(node, bytes) and len(node) == 20 and not bootstrap:
                    answered.append((node, address, reply))
                    _remember(address, node)
                for found_id, found_address in compact_nodes(reply.get(b"nodes", b"") if isinstance(reply.get(b"nodes"), bytes) else b""):
                    candidates.setdefault(found_address, found_id)

        while time.monotonic() - started < self._deadline and queries < self._max_queries:
            with lock:
                closest_answered = sorted(answered, key=lambda item: distance(item[0], target))[:K]
                bound = distance(closest_answered[-1][0], target) if len(closest_answered) >= K else None
                waiting = [a for a, n in candidates.items() if a not in asked]
                waiting.sort(key=lambda a: distance(candidates[a], target) if candidates[a] else 0)
                if bound is not None:
                    waiting = [a for a in waiting if candidates[a] is not None and distance(candidates[a], target) < bound]
            if not waiting:
                with lock:
                    nothing_yet = not answered
                if nothing_yet and bootstrap_tries < 3 and seeds:
                    # nobody but the routers was reached: ask the routers again (UDP gets lost)
                    bootstrap_tries += 1
                    asked.difference_update(seeds)
                    continue
                break
            batch = waiting[:ALPHA]
            asked.update(batch)
            queries += len(batch)
            threads = [threading.Thread(target=ask, args=(address,), daemon=True) for address in batch]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(max(2.5, self._timeout) + 0.5)
        with lock:
            return sorted(answered, key=lambda item: distance(item[0], target))[:2 * K]

    def closest(self, target, count=2 * K):
        """Ask the nodes nearest `target` for what they keep there, in two steps. First walk towards it with find_node,
        which every node answers; then send "get" to the `count` closest found. Walking with "get" alone stopped early
        on the live DHT (measured: found the record 1 time in 4), because many nodes do not answer "get" (BEP 44 is an
        extension). Returns [(node id, address, get reply)], nearest first."""
        found = self.lookup(target, "find_node")
        results, lock = [], threading.Lock()

        def ask(node, address):
            reply = self.query(address, "get", {"target": target})
            if reply is not None:
                with lock:
                    results.append((node, address, reply))

        threads = [threading.Thread(target=ask, args=(node, address), daemon=True) for node, address, _ in found[:count]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(self._timeout + 0.5)
        with lock:
            return sorted(results, key=lambda item: distance(item[0], target))

    # BEP 44

    def get_mutable(self, public_key, salt=b""):
        """The newest valid (seq, value) stored for this key and salt, or None. Every answer's signature is checked."""
        target = target_for(public_key, salt)
        best = None
        for _, _, reply in self.closest(target):
            value, seq, signature, key = reply.get(b"v"), reply.get(b"seq"), reply.get(b"sig"), reply.get(b"k")
            if value is None or not isinstance(seq, int) or key != bytes(public_key) or not isinstance(signature, bytes):
                continue
            if check_item(public_key, salt, seq, value, signature) and (best is None or seq > best[0]):
                best = (seq, value)
        return best

    def put_mutable(self, private_key, value, seq, salt=b""):
        """Store a signed record on the nodes closest to it. Returns how many nodes took it. `private_key` is an
        Ed25519PrivateKey (cryptography). `seq` must grow with every new value for the same key and salt."""
        if len(bencode(value)) > MAX_VALUE:
            raise DhtError("a DHT record holds 1000 bytes at most")
        if len(salt) > MAX_SALT:
            raise DhtError("the salt is 64 bytes at most")
        from cryptography.hazmat.primitives import serialization

        public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        signature = private_key.sign(signed_bytes(seq, value, salt))
        stored = 0
        for node, address, reply in self.closest(target_for(public_key, salt))[:K]:
            token = reply.get(b"token")
            if not isinstance(token, bytes):
                continue
            arguments = {"k": public_key, "seq": seq, "sig": signature, "token": token, "v": value}
            if salt:
                arguments["salt"] = salt
            if self.query(address, "put", arguments) is not None:
                stored += 1
        return stored
