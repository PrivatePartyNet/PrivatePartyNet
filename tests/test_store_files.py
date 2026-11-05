"""Files of any kind in the store: written in sealed pieces as they arrive, read back from any byte, and refused when a
piece is changed, moved or cut off. Also the count of what a hosted server keeps, and deleting a message."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from privatepartynet.store import FILE_CHUNK, SEAL_OVERHEAD, Store
from privatepartynet.vault import Vault, VaultError

FAST = {"n": 2 ** 10, "r": 8, "p": 1}


class StoreFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.store = Store(Vault(self.folder / "keys", scrypt_params=FAST), self.folder / "store")
        self.addCleanup(self.store.close)

    def put(self, data, step=65536):
        writer = self.store.begin_file()
        for i in range(0, len(data), step):
            writer.write(data[i:i + step])
        return writer.finish()

    def read(self, file_id, start=0, end=None):
        return b"".join(self.store.read_file(file_id, start, end))

    def test_a_file_round_trips_whatever_its_size(self):
        for size in (1, FILE_CHUNK - 1, FILE_CHUNK, FILE_CHUNK + 1, 3 * FILE_CHUNK + 12345):
            data = os.urandom(size)
            with self.subTest(size=size):
                file_id, stored, digest = self.put(data)
                self.assertEqual((stored, digest), (size, hashlib.sha256(data).hexdigest()))
                self.assertEqual(self.store.file_size(file_id), size)
                self.assertEqual(self.read(file_id), data)
                on_disk = self.store._file_path(file_id).stat().st_size
                pieces = (size + FILE_CHUNK - 1) // FILE_CHUNK
                self.assertEqual(on_disk, size + pieces * SEAL_OVERHEAD)

    def test_any_range_reads_the_right_bytes(self):
        data = os.urandom(2 * FILE_CHUNK + 777)
        file_id = self.put(data, step=100_000)[0]
        for start, end in ((0, 10), (FILE_CHUNK - 5, FILE_CHUNK + 5), (FILE_CHUNK, 2 * FILE_CHUNK), (2 * FILE_CHUNK + 700, None),
                           (5, len(data) + 99), (len(data), None), (300, 300)):
            with self.subTest(start=start, end=end):
                self.assertEqual(self.read(file_id, start, end), data[start:end])

    def test_nothing_readable_is_written_to_disk(self):
        marker = b"the launch code is swordfish " * 4000
        file_id = self.put(marker)[0]
        self.assertNotIn(b"swordfish", self.store._file_path(file_id).read_bytes())

    def test_a_changed_cut_or_reordered_file_is_refused(self):
        data = os.urandom(2 * FILE_CHUNK + 10)
        file_id = self.put(data)[0]
        path = self.store._file_path(file_id)
        good = path.read_bytes()
        piece = FILE_CHUNK + SEAL_OVERHEAD
        damaged = {
            "a changed byte": good[:500] + bytes([good[500] ^ 1]) + good[501:],
            "cut at a piece boundary": good[:2 * piece],
            "two pieces swapped": good[piece:2 * piece] + good[:piece] + good[2 * piece:],
        }
        for what, content in damaged.items():
            path.write_bytes(content)
            with self.subTest(what=what), self.assertRaises(VaultError):
                self.read(file_id)
        path.write_bytes(good)
        self.assertEqual(self.read(file_id), data)

    def test_a_file_from_another_id_will_not_open(self):
        a, b = self.put(b"A" * 1000)[0], self.put(b"B" * 1000)[0]
        self.store._file_path(b).write_bytes(self.store._file_path(a).read_bytes())
        with self.assertRaises(VaultError):
            self.read(b)

    def test_an_unfinished_or_aborted_file_leaves_nothing(self):
        writer = self.store.begin_file()
        writer.write(b"half a file")
        self.assertIsNone(self.store.file_size(writer.id), "not readable before finish")
        writer.abort()
        self.assertEqual(list((self.folder / "store" / "files").rglob("*")), [p for p in (self.folder / "store" / "files").rglob("*") if p.is_dir()])
        with self.assertRaises(ValueError):
            self.store.begin_file().finish()

    def test_unknown_and_bad_ids(self):
        for bad in ("0" * 32, "../../keys", "", None, "A" * 32):
            with self.subTest(bad=bad):
                self.assertIsNone(self.store.file_size(bad))
                with self.assertRaises(KeyError):
                    self.store.read_file(bad)
                self.store.delete_file(bad)

    def test_a_deleted_file_is_gone(self):
        file_id = self.put(b"x" * 5000)[0]
        self.store.delete_file(file_id)
        self.assertIsNone(self.store.file_size(file_id))
        self.assertFalse(self.store._file_path(file_id).exists())

    def test_what_a_server_keeps_is_counted_and_released(self):
        self.store.hold("s1", "file", "f1", 1000, uses=1)
        self.store.hold("s1", "image", "h1", 300, uses=1)
        self.store.hold("s1", "image", "h1", 300, uses=1)  # the same picture in a second message
        self.store.hold("s2", "file", "f9", 5, uses=1)
        self.assertEqual(self.store.held_bytes("s1"), 1300, "a picture in two messages is kept once")
        self.assertEqual(self.store.largest_held("s1"), [("file", "f1", 1000), ("image", "h1", 300)])
        self.assertFalse(self.store.release("s1", "image", "h1"), "one message still uses it")
        self.assertTrue(self.store.release("s1", "image", "h1"))
        self.assertEqual(self.store.held_bytes("s1"), 1000)
        self.assertFalse(self.store.release("s1", "image", "h1"), "already gone")
        self.assertEqual(self.store.held_bytes("s2"), 5, "servers are counted apart")
        self.assertEqual(self.store.held_bytes("nobody"), 0)

    def test_a_message_can_be_read_by_id_and_deleted(self):
        self.store.add_message("m1", "srv:s1:c1", "u1", {"text": "hello"}, ts=1.0)
        self.assertEqual(self.store.message("m1", "srv:s1:c1")["body"]["text"], "hello")
        self.assertIsNone(self.store.message("m1", "srv:s1:other"), "only in its own channel")
        self.assertTrue(self.store.delete_message("m1", "srv:s1:c1"))
        self.assertFalse(self.store.delete_message("m1", "srv:s1:c1"))
        self.assertEqual(self.store.history("srv:s1:c1"), [])

    def test_a_blob_can_be_deleted_and_its_size_read(self):
        digest = self.store.put_blob(b"\x89PNG" + b"1" * 100)
        self.assertEqual(self.store.blob_size(digest), 104)
        self.store.delete_blob(digest)
        self.assertFalse(self.store.has_blob(digest))
        self.assertIsNone(self.store.blob_size(digest))


if __name__ == "__main__":
    unittest.main()
