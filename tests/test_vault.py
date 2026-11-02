import json
import os
import tempfile
import unittest
from pathlib import Path

from privatepartynet.vault import (Locked, Vault, VaultError, WrongPassword, derive, dpapi_available, dpapi_protect,
                            dpapi_unprotect, seal, unseal)

FAST = {"n": 2 ** 10, "r": 8, "p": 1}  # real settings take about a tenth of a second; tests do not need that


class SealTests(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        key = os.urandom(32)
        sealed = seal(key, b"hello", b"scope")
        self.assertEqual(unseal(key, sealed, b"scope"), b"hello")
        with self.assertRaises(VaultError):
            unseal(key, sealed, b"other scope")
        flipped = sealed[:-1] + bytes([sealed[-1] ^ 1])
        with self.assertRaises(VaultError):
            unseal(key, flipped, b"scope")
        with self.assertRaises(VaultError):
            unseal(os.urandom(32), sealed, b"scope")

    def test_two_seals_of_the_same_text_differ(self):
        key = os.urandom(32)
        self.assertNotEqual(seal(key, b"same"), seal(key, b"same"))

    def test_subkeys_differ_by_label(self):
        key = os.urandom(32)
        self.assertNotEqual(derive(key, "store"), derive(key, "identity"))
        self.assertEqual(derive(key, "store"), derive(key, "store"))


@unittest.skipUnless(dpapi_available(), "Windows data protection only")
class DpapiTests(unittest.TestCase):
    def test_round_trip_and_wrong_entropy(self):
        wrapped = dpapi_protect(b"k" * 32)
        self.assertNotIn(b"k" * 32, wrapped)
        self.assertEqual(dpapi_unprotect(wrapped), b"k" * 32)
        with self.assertRaises(VaultError):
            dpapi_unprotect(wrapped, entropy=b"another purpose")


class VaultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def vault(self):
        return Vault(self.folder, scrypt_params=FAST)

    def test_a_new_vault_is_unlocked_and_needs_no_password(self):
        vault = self.vault()
        self.assertTrue(vault.unlocked)
        self.assertFalse(vault.has_password)

    def test_the_same_key_comes_back_on_the_next_start(self):
        first = self.vault().subkey("store")
        self.assertEqual(self.vault().subkey("store"), first)

    def test_the_key_file_does_not_contain_the_key(self):
        vault = self.vault()
        master = vault._master
        text = (self.folder / "master.json").read_text(encoding="utf-8")
        self.assertNotIn(master.hex(), text)

    def test_adding_a_password_locks_a_restart_until_it_is_given(self):
        vault = self.vault()
        key = vault.subkey("store")
        vault.set_password("correct horse", None)
        again = self.vault()
        self.assertTrue(again.has_password)
        self.assertFalse(again.unlocked)
        with self.assertRaises(Locked):
            again.subkey("store")
        with self.assertRaises(WrongPassword):
            again.unlock("wrong")
        again.unlock("correct horse")
        self.assertEqual(again.subkey("store"), key, "the password changes how the key is wrapped, not the key")

    def test_changing_the_password_needs_the_old_one(self):
        vault = self.vault()
        vault.set_password("one")
        with self.assertRaises(WrongPassword):
            vault.set_password("two", "not one")
        vault.set_password("two", "one")
        again = self.vault()
        with self.assertRaises(WrongPassword):
            again.unlock("one")
        again.unlock("two")

    def test_removing_the_password_goes_back_to_windows_protection(self):
        vault = self.vault()
        key = vault.subkey("store")
        vault.set_password("pw")
        with self.assertRaises(WrongPassword):
            vault.remove_password("nope")
        vault.remove_password("pw")
        again = self.vault()
        self.assertFalse(again.has_password)
        self.assertEqual(again.subkey("store"), key)

    def test_lock_forgets_the_key_only_when_there_is_a_password(self):
        vault = self.vault()
        vault.lock()
        self.assertTrue(vault.unlocked, "no password: nothing to ask for, so locking does nothing")
        vault.set_password("pw")
        vault.lock()
        self.assertFalse(vault.unlocked)

    def test_a_damaged_key_file_is_refused_not_replaced(self):
        self.vault()
        path = self.folder / "master.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(VaultError):
            self.vault()
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json", "a damaged key must not be overwritten")

    @unittest.skipUnless(dpapi_available(), "Windows data protection only")
    def test_a_key_file_from_another_machine_is_refused(self):
        self.vault()
        path = self.folder / "master.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        wrapped = bytearray(bytes.fromhex(record["wrapped"]))
        wrapped[len(wrapped) // 2] ^= 0xFF
        record["wrapped"] = bytes(wrapped).hex()
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(VaultError):
            self.vault()


if __name__ == "__main__":
    unittest.main()
