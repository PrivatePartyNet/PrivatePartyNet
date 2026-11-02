"""The DHT client against BEP 44's own test vectors, and against a small DHT of fake nodes on this PC."""

import os
import socket
import struct
import threading
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from privatepartynet import dht
from privatepartynet.dht import Dht, DhtError, bdecode, bencode, check_item, compact_nodes, distance, signed_bytes, target_for

VECTOR_KEY = bytes.fromhex("77ff84905a91936367c01360803104f92432fcd904a43511876df5cdf3e7e548")
VECTOR_SIG = bytes.fromhex("305ac8aeb6c9c151fa120f120ea2cfb923564e11552d06a5d856091e5e853cff"
                           "1260d3f39e4999684aa92eb73ffd136e6f4f3ecbfda0ce53a1608ecd7ae21f01")
VECTOR_SALT_SIG = bytes.fromhex("6834284b6b24c3204eb2fea824d82f88883a3d95e8b4a21b8c0ded553d17d17d"
                                "df9a8a7104b1258f30bed3787e6cb896fca78c58f8e03b5f18f14951a87d9a08")


class BencodeTests(unittest.TestCase):
    def test_round_trip_with_sorted_keys(self):
        value = {"t": b"aa", "a": {"id": b"x" * 20, "n": 5, "list": [1, b"two", {"z": 0}]}}
        encoded = bencode(value)
        self.assertTrue(encoded.startswith(b"d1:ad"), "keys are sorted")
        decoded = bdecode(encoded)
        self.assertEqual(decoded[b"a"][b"list"], [1, b"two", {b"z": 0}])

    def test_malformed_input_is_refused(self):
        for bad in (b"", b"i01e", b"i-0e", b"5:abc", b"d3:abc", b"l", b"x", b"i5ei6e", b"d" * 40 + b"e" * 40):
            with self.assertRaises((DhtError, ValueError), msg=bad):
                bdecode(bad)

    def test_booleans_and_unknown_types_are_refused(self):
        with self.assertRaises(TypeError):
            bencode(True)
        with self.assertRaises(TypeError):
            bencode(1.5)


class Bep44VectorTests(unittest.TestCase):
    def test_the_signed_bytes_match_the_specification(self):
        self.assertEqual(signed_bytes(1, b"Hello World!"), b"3:seqi1e1:v12:Hello World!")
        self.assertEqual(signed_bytes(1, b"Hello World!", b"foobar"), b"4:salt6:foobar3:seqi1e1:v12:Hello World!")

    def test_the_targets_match_the_specification(self):
        self.assertEqual(target_for(VECTOR_KEY).hex(), "4a533d47ec9c7d95b1ad75f576cffc641853b750")
        self.assertEqual(target_for(VECTOR_KEY, b"foobar").hex(), "411eba73b6f087ca51a3795d9c8c938d365e32c1")

    def test_the_specifications_signatures_verify(self):
        self.assertTrue(check_item(VECTOR_KEY, b"", 1, b"Hello World!", VECTOR_SIG))
        self.assertTrue(check_item(VECTOR_KEY, b"foobar", 1, b"Hello World!", VECTOR_SALT_SIG))
        self.assertFalse(check_item(VECTOR_KEY, b"", 2, b"Hello World!", VECTOR_SIG), "another seq")
        self.assertFalse(check_item(VECTOR_KEY, b"", 1, b"Hello World?", VECTOR_SIG), "another value")
        self.assertFalse(check_item(VECTOR_KEY, b"other", 1, b"Hello World!", VECTOR_SALT_SIG), "another salt")


def compact(node_id, address):
    return node_id + socket.inet_aton(address[0]) + struct.pack(">H", address[1])


class FakeNode:
    """One DHT node on loopback that answers get and put as BEP 5 and BEP 44 say, and knows the others in `network`."""

    def __init__(self, network):
        self.id = os.urandom(20)
        self.network = network
        self.items = {}
        self.tokens = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.address = self.sock.getsockname()
        self.queries = 0
        self.silent = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def closest(self, target):
        return sorted(self.network, key=lambda node: distance(node.id, target))[:8]

    def serve(self):
        while not self._stop.is_set():
            try:
                data, source = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            self.queries += 1
            if self.silent:
                continue
            message = bdecode(data)
            arguments = message[b"a"]
            reply = {"id": self.id}
            if message[b"q"] == b"find_node":
                reply.update(nodes=b"".join(compact(n.id, n.address) for n in self.closest(arguments[b"target"])))
            elif message[b"q"] == b"get":
                target = arguments[b"target"]
                token = os.urandom(4)
                self.tokens.add(token)
                reply.update(token=token, nodes=b"".join(compact(n.id, n.address) for n in self.closest(target)))
                if target in self.items:
                    seq, value, signature, key = self.items[target]
                    reply.update(seq=seq, v=value, sig=signature, k=key)
            elif message[b"q"] == b"put":
                key, salt = arguments[b"k"], arguments.get(b"salt", b"")
                if arguments[b"token"] not in self.tokens or not check_item(key, salt, arguments[b"seq"], arguments[b"v"], arguments[b"sig"]):
                    self.sock.sendto(bencode({"t": message[b"t"], "y": "e", "e": [206, "invalid"]}), source)
                    continue
                target = target_for(key, salt)
                current = self.items.get(target)
                if current is None or arguments[b"seq"] > current[0]:
                    self.items[target] = (arguments[b"seq"], arguments[b"v"], arguments[b"sig"], key)
            self.sock.sendto(bencode({"t": message[b"t"], "y": "r", "r": reply}), source)

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


class FakeDhtTests(unittest.TestCase):
    def setUp(self):
        dht._known.clear()  # nodes remembered from another test are not in this one's network
        self.network = []
        for _ in range(30):
            self.network.append(FakeNode(self.network))
        for node in self.network:
            self.addCleanup(node.close)

    def client(self, **options):
        options.setdefault("timeout", 0.5)
        options.setdefault("deadline", 10.0)
        client = Dht(bootstrap=[self.network[0].address], **options)
        self.addCleanup(client.close)
        return client

    def test_a_record_put_by_one_client_is_read_by_another(self):
        key = Ed25519PrivateKey.generate()
        writer = self.client()
        stored = writer.put_mutable(key, {"a": "203.0.113.5", "p": 40000}, seq=1, salt=b"privatepartynet-host")
        self.assertGreaterEqual(stored, 4)
        public = key.public_key().public_bytes_raw()
        found = self.client().get_mutable(public, b"privatepartynet-host")
        self.assertEqual(found, (1, {b"a": b"203.0.113.5", b"p": 40000}))

    def test_the_newest_seq_wins_and_another_salt_is_another_record(self):
        key = Ed25519PrivateKey.generate()
        client = self.client()
        client.put_mutable(key, b"old", seq=1, salt=b"s")
        client.put_mutable(key, b"new", seq=2, salt=b"s")
        public = key.public_key().public_bytes_raw()
        self.assertEqual(client.get_mutable(public, b"s"), (2, b"new"))
        self.assertIsNone(client.get_mutable(public, b"other"))

    def test_a_forged_record_on_a_lying_node_is_ignored(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        target = target_for(public, b"s")
        liar = min(self.network, key=lambda node: distance(node.id, target))
        liar.items[target] = (99, b"forged", os.urandom(64), public)
        self.client().put_mutable(key, b"real", seq=1, salt=b"s")
        self.assertEqual(self.client().get_mutable(public, b"s"), (1, b"real"))

    def test_silent_nodes_and_the_deadline_bound_a_lookup(self):
        import time

        for node in self.network[1:]:
            node.silent = True
        started = time.monotonic()
        result = self.client(deadline=3.0).lookup(os.urandom(20))
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result, [], "the only node that answers is the bootstrap one, which holds no records")

    def test_the_query_count_is_bounded(self):
        client = self.client(max_queries=12)
        client.lookup(os.urandom(20))
        self.assertLessEqual(sum(node.queries for node in self.network), 12 + dht.ALPHA)

    def test_too_big_a_record_is_refused_before_sending(self):
        with self.assertRaises(DhtError):
            self.client().put_mutable(Ed25519PrivateKey.generate(), b"x" * 1000, seq=1)

    def test_compact_nodes_skip_zero_ports_and_partial_entries(self):
        good = compact(b"a" * 20, ("10.0.0.1", 6881))
        bad = compact(b"b" * 20, ("10.0.0.2", 0))
        self.assertEqual(compact_nodes(good + bad + b"xx"), [(b"a" * 20, ("10.0.0.1", 6881))])


if __name__ == "__main__":
    unittest.main()
