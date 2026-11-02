import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet.store import Store, blob_hash
from privatepartynet.vault import Vault, VaultError

FAST = {"n": 2 ** 10, "r": 8, "p": 1}
SECRET = "the launch code is swordfish"


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.vault = Vault(self.folder / "keys", scrypt_params=FAST)
        self.store = Store(self.vault, self.folder / "store")
        self.addCleanup(self.store.close)

    def add(self, number, scope="srv:s1:general", **extra):
        return self.store.add_message(f"m{number:04d}", scope, "u1", {"text": f"message {number}", "name": "Kay"},
                                      ts=1000.0 + number, **extra)

    def test_a_message_round_trips(self):
        self.store.add_message("a", "dm:u2", "u1", {"text": SECRET, "name": "Kay"}, ts=5.0, seq=7, sig=b"sig")
        (got,) = self.store.history("dm:u2")
        self.assertEqual((got["id"], got["seq"], got["ts"], got["sender"], got["sig"]), ("a", 7, 5.0, "u1", b"sig"))
        self.assertEqual(got["body"], {"text": SECRET, "name": "Kay"})

    def test_nothing_readable_is_written_to_disk(self):
        self.store.add_message("a", "dm:u2", "u1", {"text": SECRET, "name": "Kay"})
        self.store.put_blob(b"PICTURE-BYTES-" + bytes(300))
        self.store.save_contact({"id": "u2", "name": "Secret Name Zed", "sign": "aa", "dh": "bb"})
        self.store.close()
        everything = b"".join(path.read_bytes() for path in (self.folder / "store").rglob("*") if path.is_file())
        for needle in (SECRET.encode(), b"Secret Name Zed", b"PICTURE-BYTES-", b"swordfish"):
            self.assertNotIn(needle, everything)
        self.store = Store(self.vault, self.folder / "store")
        self.addCleanup(self.store.close)

    def test_an_id_is_stored_once(self):
        self.assertTrue(self.add(1))
        self.assertFalse(self.add(1), "the second insert of the same id is a no-op")
        self.assertEqual(self.store.count(), 1)

    def test_history_pages_back_from_the_newest(self):
        for number in range(1, 11):
            self.add(number)
        newest = self.store.history("srv:s1:general", limit=3)
        self.assertEqual([m["id"] for m in newest], ["m0008", "m0009", "m0010"])
        older = self.store.history("srv:s1:general", limit=3, before_ts=newest[0]["ts"])
        self.assertEqual([m["id"] for m in older], ["m0005", "m0006", "m0007"])

    def test_scopes_are_kept_apart(self):
        self.add(1, scope="srv:s1:general")
        self.add(2, scope="srv:s1:random")
        self.add(3, scope="dm:u2")
        self.assertEqual(self.store.scopes(), ["dm:u2", "srv:s1:general", "srv:s1:random"])
        self.assertEqual(self.store.count("dm:u2"), 1)

    def test_a_rejoining_member_gets_only_the_gap(self):
        for number in range(1, 7):
            self.add(number, seq=number)
        self.assertEqual(self.store.last_seq("srv:s1:general"), 6)
        gap = self.store.since_seq("srv:s1:general", 4)
        self.assertEqual([m["seq"] for m in gap], [5, 6])
        self.assertEqual(self.store.since_seq("srv:s1:general", 6), [])

    def test_a_row_moved_to_another_conversation_will_not_open(self):
        self.add(1, scope="dm:u2")
        self.store._db.execute("update messages set scope='dm:u3'")
        self.store._db.commit()
        with self.assertRaises(VaultError):
            self.store.history("dm:u3")

    def test_unread_counts_follow_the_read_marker(self):
        for number in range(1, 5):
            self.add(number)
        self.assertEqual(self.store.unread("srv:s1:general"), 4)
        self.store.mark_read("srv:s1:general", ts=1002.5)
        self.assertEqual(self.store.unread("srv:s1:general"), 2)

    def test_contact_keys_are_pinned_on_first_sight(self):
        card = {"id": "u2", "name": "Bob", "sign": "aa", "dh": "bb"}
        self.store.save_contact(card)
        self.store.save_contact(dict(card, name="Robert"))  # a rename is fine
        self.assertEqual(self.store.contact("u2")["name"], "Robert")
        with self.assertRaises(VaultError):
            self.store.save_contact(dict(card, sign="cc"))  # new keys under a known id are not
        self.assertEqual(self.store.contact("u2")["sign"], "aa")
        self.assertEqual(len(self.store.contacts()), 1)

    def test_servers_are_saved_and_forgotten(self):
        self.store.save_server("s1", {"name": "Heroes night", "addresses": ["1.2.3.4:8765"]})
        self.assertEqual(self.store.server("s1")["name"], "Heroes night")
        self.assertEqual([s["name"] for s in self.store.servers()], ["Heroes night"])
        self.store.forget_server("s1")
        self.assertIsNone(self.store.server("s1"))

    def test_the_outbox_retries_with_backoff_and_clears_when_sent(self):
        self.store.queue_outgoing("o1", "dm:u2", "u2", {"text": "hi"})
        (item,) = self.store.due_outgoing(now=time.time() + 1)
        self.assertEqual(item["payload"], {"text": "hi"})
        self.store.failed("o1", now=1000.0)
        self.assertEqual(self.store.due_outgoing(now=1004.0), [], "5 seconds after the first failure")
        self.assertEqual(len(self.store.due_outgoing(now=1006.0)), 1)
        self.store.failed("o1", now=1006.0)
        self.assertEqual(self.store.due_outgoing(now=1015.0), [], "10 seconds after the second")
        self.store.sent("o1")
        self.assertEqual(self.store.outbox_count(), 0)

    def test_blobs_are_named_by_hash_and_checked_on_the_way_out(self):
        data = b"\x89PNG" + bytes(range(256))
        digest = self.store.put_blob(data)
        self.assertEqual(digest, blob_hash(data))
        self.assertTrue(self.store.has_blob(digest))
        self.assertEqual(self.store.get_blob(digest), data)
        self.assertEqual(self.store.put_blob(data), digest, "the same bytes are stored once")
        self.assertEqual(self.store.blob_hashes(), [digest])
        self.assertIsNone(self.store.get_blob("0" * 64))
        path = self.store._blob_path(digest)
        path.write_bytes(path.read_bytes()[:-1] + b"\x00")
        with self.assertRaises(VaultError):
            self.store.get_blob(digest)

    def test_a_fifty_thousand_message_history_stays_quick(self):
        rows = [(f"b{i}", "srv:s1:general", None, 2000.0 + i, "u1", self.store._seal(
            {"text": f"message {i}", "name": "Kay"}, self.store._message_aad("srv:s1:general", f"b{i}")), None)
            for i in range(50000)]
        self.store._db.executemany("insert into messages(id, scope, seq, ts, sender, body, sig) values(?,?,?,?,?,?,?)", rows)
        self.store._db.commit()
        start = time.perf_counter()
        newest = self.store.history("srv:s1:general", limit=200)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(newest), 200)
        self.assertEqual(newest[-1]["id"], "b49999")
        self.assertLess(elapsed, 0.5)

    def test_the_wrong_key_reads_nothing(self):
        self.add(1)
        other = Store(Vault(self.folder / "other-keys", scrypt_params=FAST), self.folder / "store")
        self.addCleanup(other.close)
        with self.assertRaises(VaultError):
            other.history("srv:s1:general")


if __name__ == "__main__":
    unittest.main()
