import tempfile
import unittest
from pathlib import Path

from privatepartynet.identity import Identity, safety_number, user_id_of, verify
from privatepartynet.vault import Vault, VaultError

FAST = {"n": 2 ** 10, "r": 8, "p": 1}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.alice = Identity.create()
        self.bob = Identity.create()

    def test_the_id_comes_from_both_public_keys(self):
        self.assertEqual(self.alice.user_id, user_id_of(self.alice.sign_public, self.alice.dh_public))
        self.assertEqual(len(self.alice.user_id), 32)
        self.assertNotEqual(self.alice.user_id, self.bob.user_id)
        self.assertNotEqual(user_id_of(self.alice.sign_public, self.bob.dh_public), self.alice.user_id)

    def test_signatures_verify_only_for_the_signer_and_the_data(self):
        signature = self.alice.sign(b"data")
        self.assertTrue(verify(self.alice.sign_public, signature, b"data"))
        self.assertFalse(verify(self.alice.sign_public, signature, b"other"))
        self.assertFalse(verify(self.bob.sign_public, signature, b"data"))
        self.assertFalse(verify(self.alice.sign_public, b"short", b"data"))

    def test_a_sealed_message_opens_for_the_recipient_only(self):
        sealed = self.alice.seal_to(self.bob.dh_public, b"hi bob", b"dm")
        self.assertEqual(self.bob.open_from(self.alice.sign_public, sealed, b"dm"), b"hi bob")
        carol = Identity.create()
        with self.assertRaises(VaultError):
            carol.open_from(self.alice.sign_public, sealed, b"dm")

    def test_a_sealed_message_is_tied_to_its_sender_and_context(self):
        sealed = self.alice.seal_to(self.bob.dh_public, b"hi bob", b"dm")
        with self.assertRaises(VaultError):
            self.bob.open_from(Identity.create().sign_public, sealed, b"dm")  # claimed to be someone else
        with self.assertRaises(VaultError):
            self.bob.open_from(self.alice.sign_public, sealed, b"channel")  # moved to another conversation

    def test_a_flipped_byte_is_refused(self):
        sealed = bytearray(self.alice.seal_to(self.bob.dh_public, b"hi bob"))
        sealed[50] ^= 1
        with self.assertRaises(VaultError):
            self.bob.open_from(self.alice.sign_public, bytes(sealed))

    def test_two_seals_of_the_same_text_differ(self):
        self.assertNotEqual(self.alice.seal_to(self.bob.dh_public, b"x"), self.alice.seal_to(self.bob.dh_public, b"x"))

    def test_the_safety_number_matches_on_both_sides(self):
        a, b = self.alice.card("Alice"), self.bob.card("Bob")
        self.assertEqual(safety_number(a, b), safety_number(b, a))
        self.assertEqual(len(safety_number(a, b).split()), 12)
        self.assertNotEqual(safety_number(a, b), safety_number(a, Identity.create().card()))

    def test_the_identity_survives_a_restart_and_is_not_stored_in_the_clear(self):
        vault = Vault(self.folder / "keys", scrypt_params=FAST)
        first = Identity.load_or_create(vault, self.folder / "identity")
        raw = (self.folder / "identity" / "identity.bin").read_bytes()
        self.assertNotIn(first.private_bundle(), raw)
        self.assertNotIn(first.sign_public, raw, "even the public key is sealed on disk")
        second = Identity.load_or_create(Vault(self.folder / "keys", scrypt_params=FAST), self.folder / "identity")
        self.assertEqual(first.user_id, second.user_id)
        self.assertEqual(second.open_from(first.sign_public, first.seal_to(second.dh_public, b"me")), b"me")

    def test_an_identity_can_move_with_its_private_bundle(self):
        moved = Identity.from_private_bundle(self.alice.private_bundle())
        self.assertEqual(moved.user_id, self.alice.user_id)
        with self.assertRaises(VaultError):
            Identity.from_private_bundle(b"garbage")


if __name__ == "__main__":
    unittest.main()
