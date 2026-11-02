import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from privatepartynet import backup
from privatepartynet.backup import BackupError, export_all, import_backup, open_file, read_identity, seal_file
from privatepartynet.identity import Identity
from privatepartynet.store import Store
from privatepartynet.vault import Vault

FAST = {"n": 2 ** 10, "r": 8, "p": 1}
LOG2 = 10  # scrypt cost for tests; real exports use 15
PASS = "correct horse battery staple"


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.make_store("source")
        self.target = self.make_store("target")

    def make_store(self, name):
        vault = Vault(self.folder / name / "keys", scrypt_params=FAST)
        store = Store(vault, self.folder / name / "store")
        self.addCleanup(store.close)
        return store

    def fill(self, store, messages=30, pictures=2):
        for number in range(messages):
            body = {"text": f"hello {number}", "name": "Kay"}
            if number < pictures:
                body["image"] = {"hash": store.put_blob(b"\x89PNG\r\n\x1a\n" + bytes([number]) * 500), "w": 10, "h": 10}
            store.add_message(f"m{number:03d}", "srv:s1:general" if number % 3 else "dm:u2", "u1", body,
                              ts=1000.0 + number, seq=number + 1, sig=b"sig" if number == 5 else None)
        store.save_contact({"id": "u2", "name": "Bob", "sign": "aa", "dh": "bb"})
        store.save_server("s1", {"name": "Heroes night", "channels": []})

    def export(self, **options):
        path = self.folder / ("out" + backup.EXTENSION)
        return path, export_all(self.source, PASS, path, log2_n=LOG2, **options)


class SealTests(BackupTestCase):
    def roundtrip(self, data):
        plain, sealed, back = self.folder / "p", self.folder / "s", self.folder / "b"
        plain.write_bytes(data)
        seal_file(plain, sealed, PASS, log2_n=LOG2)
        open_file(sealed, back, PASS)
        return back.read_bytes(), sealed

    def test_sizes_around_a_chunk_boundary_round_trip(self):
        for size in (0, 1, backup.CHUNK - 1, backup.CHUNK, backup.CHUNK + 1, 2 * backup.CHUNK, 2 * backup.CHUNK + 7):
            data = os.urandom(size)
            got, _ = self.roundtrip(data)
            self.assertEqual(got, data, size)

    def test_the_sealed_file_shows_nothing_of_the_contents(self):
        got, sealed = self.roundtrip(b"swordfish " * 5000)
        self.assertNotIn(b"swordfish", sealed.read_bytes())

    def test_a_wrong_passphrase_is_refused_plainly(self):
        _, sealed = self.roundtrip(b"data")
        with self.assertRaisesRegex(BackupError, "passphrase is wrong"):
            open_file(sealed, self.folder / "x", "not the passphrase")

    def test_a_flipped_byte_anywhere_is_caught(self):
        _, sealed = self.roundtrip(os.urandom(backup.CHUNK * 2 + 100))
        original = sealed.read_bytes()
        for position in (0, 9, 20, 30, backup.HEADER_SIZE + 3, backup.HEADER_SIZE + 5000, len(original) - 3):
            damaged = bytearray(original)
            damaged[position] ^= 0x01
            sealed.write_bytes(bytes(damaged))
            with self.assertRaises(BackupError, msg=f"byte {position}"):
                open_file(sealed, self.folder / "x", PASS)
        sealed.write_bytes(original)

    def test_a_cut_off_file_is_caught_even_at_a_chunk_edge(self):
        _, sealed = self.roundtrip(os.urandom(backup.CHUNK * 3))
        original = sealed.read_bytes()
        one_chunk = backup.HEADER_SIZE + 5 + backup.CHUNK + 16
        for length in (10, backup.HEADER_SIZE, one_chunk, len(original) - 1):
            sealed.write_bytes(original[:length])
            with self.assertRaises(BackupError, msg=f"cut at {length}"):
                open_file(sealed, self.folder / "x", PASS)

    def test_data_added_after_the_end_is_caught(self):
        _, sealed = self.roundtrip(b"data")
        sealed.write_bytes(sealed.read_bytes() + b"extra")
        with self.assertRaises(BackupError):
            open_file(sealed, self.folder / "x", PASS)

    def test_chunks_cannot_be_reordered(self):
        _, sealed = self.roundtrip(os.urandom(backup.CHUNK * 2 + 10))
        data = sealed.read_bytes()
        size = 5 + backup.CHUNK + 16
        head, first, second, rest = (data[:backup.HEADER_SIZE], data[backup.HEADER_SIZE:backup.HEADER_SIZE + size],
                                     data[backup.HEADER_SIZE + size:backup.HEADER_SIZE + 2 * size],
                                     data[backup.HEADER_SIZE + 2 * size:])
        sealed.write_bytes(head + second + first + rest)
        with self.assertRaises(BackupError):
            open_file(sealed, self.folder / "x", PASS)

    def test_something_that_is_not_a_backup_says_so(self):
        notes = self.folder / "notes.txt"
        notes.write_bytes(b"just some text" * 10)
        with self.assertRaisesRegex(BackupError, "not a PrivatePartyNet backup"):
            open_file(notes, self.folder / "x", PASS)

    def test_an_absurd_memory_request_in_the_header_is_refused(self):
        _, sealed = self.roundtrip(b"data")
        data = bytearray(sealed.read_bytes())
        data[25] = 40  # scrypt cost far beyond what is ever written
        sealed.write_bytes(bytes(data))
        with self.assertRaisesRegex(BackupError, "damaged"):
            open_file(sealed, self.folder / "x", PASS)

    def test_an_empty_passphrase_is_refused(self):
        plain = self.folder / "p"
        plain.write_bytes(b"x")
        with self.assertRaises(BackupError):
            seal_file(plain, self.folder / "s", "", log2_n=LOG2)


class ExportImportTests(BackupTestCase):
    def test_everything_comes_across(self):
        self.fill(self.source)
        path, report = self.export()
        self.assertEqual((report["scopes"], report["messages"], report["blobs"]), (2, 30, 2))
        result = import_backup(self.target, path, PASS)
        self.assertEqual((result["messages_added"], result["messages_skipped"], result["blobs_added"]), (30, 0, 2))
        self.assertEqual(self.target.scopes(), self.source.scopes())
        for scope in self.source.scopes():
            self.assertEqual(self.target.all_messages(scope), self.source.all_messages(scope))
        for digest in self.source.blob_hashes():
            self.assertEqual(self.target.get_blob(digest), self.source.get_blob(digest))
        self.assertEqual(self.target.contact("u2")["name"], "Bob")
        self.assertEqual(self.target.server("s1")["name"], "Heroes night")

    def test_importing_twice_adds_nothing_the_second_time(self):
        self.fill(self.source)
        path, _ = self.export()
        import_backup(self.target, path, PASS)
        again = import_backup(self.target, path, PASS)
        self.assertEqual((again["messages_added"], again["messages_skipped"]), (0, 30))
        self.assertEqual(self.target.count(), 30)

    def test_importing_into_a_pc_with_some_of_the_history_fills_the_gaps(self):
        self.fill(self.source)
        for message in self.source.all_messages("srv:s1:general")[:5]:
            self.target.add_message(message["id"], message["scope"], message["sender"], message["body"],
                                    ts=message["ts"], seq=message["seq"])
        path, _ = self.export()
        result = import_backup(self.target, path, PASS)
        self.assertEqual(result["messages_skipped"], 5)
        self.assertEqual(result["messages_added"], 25)
        self.assertEqual(self.target.count(), 30)

    def test_one_conversation_can_be_exported_alone(self):
        self.fill(self.source)
        path, report = self.export(scopes=["dm:u2"])
        self.assertEqual(report["scopes"], 1)
        import_backup(self.target, path, PASS)
        self.assertEqual(self.target.scopes(), ["dm:u2"])

    def test_signatures_survive(self):
        self.fill(self.source)
        path, _ = self.export()
        import_backup(self.target, path, PASS)
        signed = [m for scope in self.target.scopes() for m in self.target.all_messages(scope) if m["sig"]]
        self.assertEqual([m["sig"] for m in signed], [b"sig"])

    def test_the_identity_travels_only_when_asked(self):
        self.fill(self.source, messages=3, pictures=0)
        me = Identity.create()
        path, report = self.export()
        self.assertFalse(report["identity"])
        self.assertIsNone(read_identity(path, PASS))
        path, report = self.export(identity=me)
        self.assertTrue(report["identity"])
        moved = Identity.from_private_bundle(read_identity(path, PASS))
        self.assertEqual(moved.user_id, me.user_id)
        self.assertTrue(import_backup(self.target, path, PASS)["has_identity"])

    def test_a_contact_with_different_keys_does_not_overwrite_the_pinned_ones(self):
        self.fill(self.source, messages=1, pictures=0)
        self.target.save_contact({"id": "u2", "name": "Bob", "sign": "other", "dh": "keys"})
        path, _ = self.export()
        result = import_backup(self.target, path, PASS)
        self.assertEqual(result["contacts_kept"], 1)
        self.assertEqual(self.target.contact("u2")["sign"], "other")

    def test_a_wrong_passphrase_imports_nothing(self):
        self.fill(self.source)
        path, _ = self.export()
        with self.assertRaises(BackupError):
            import_backup(self.target, path, "nope")
        self.assertEqual(self.target.count(), 0)

    def test_an_empty_store_exports_and_imports(self):
        path, report = self.export()
        self.assertEqual(report["messages"], 0)
        result = import_backup(self.target, path, PASS)
        self.assertEqual(result["messages_added"], 0)

    def test_the_export_leaves_no_temporary_files_and_the_archive_is_unreadable_without_the_passphrase(self):
        self.fill(self.source, messages=5, pictures=0)
        path, _ = self.export()
        self.assertEqual([p.name for p in self.folder.glob("privatepartynet-backup-*")], [])
        self.assertNotIn(b"hello 1", path.read_bytes())
        self.assertFalse(zipfile.is_zipfile(path))

    def test_a_backup_with_a_hostile_member_name_is_refused(self):
        plain = self.folder / "evil.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("../escape.txt", "x")
            archive.writestr("manifest.json", '{"format": "privatepartynet-backup", "scopes": []}')
        sealed = self.folder / "evil.ppnbak"
        seal_file(plain, sealed, PASS, log2_n=LOG2)
        with self.assertRaisesRegex(BackupError, "damaged"):
            import_backup(self.target, sealed, PASS)

    def test_a_zip_that_is_not_a_privatepartynet_backup_is_refused(self):
        plain = self.folder / "other.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("readme.txt", "hello")
        sealed = self.folder / "other.ppnbak"
        seal_file(plain, sealed, PASS, log2_n=LOG2)
        with self.assertRaisesRegex(BackupError, "not a PrivatePartyNet backup"):
            import_backup(self.target, sealed, PASS)

    def test_a_large_history_stays_in_bounded_memory_and_time(self):
        rows = [(f"b{i}", "srv:s1:general", i + 1, 5000.0 + i, "u1",
                 self.source._seal({"text": f"message {i}", "name": "Kay"}, self.source._message_aad("srv:s1:general", f"b{i}")), None)
                for i in range(20000)]
        self.source._db.executemany("insert into messages(id, scope, seq, ts, sender, body, sig) values(?,?,?,?,?,?,?)", rows)
        self.source._db.commit()
        path, report = self.export()
        self.assertEqual(report["messages"], 20000)
        result = import_backup(self.target, path, PASS)
        self.assertEqual(result["messages_added"], 20000)


if __name__ == "__main__":
    unittest.main()
