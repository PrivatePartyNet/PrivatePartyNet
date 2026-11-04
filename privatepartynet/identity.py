"""Who this install is.

Each install has two key pairs: an Ed25519 pair that signs (messages, challenges, join requests) and an X25519 pair
that agrees on secrets (direct messages, channel keys). `user_id` is the first 16 bytes of the SHA-256 of both public
keys, in hex, so a person's id cannot be claimed without their keys. The private keys are stored sealed by the vault.

Sealing to someone (`seal_to`) uses a fresh ephemeral X25519 key for every message, and the sender signs the result, so
the recipient knows who it came from. This gives no forward secrecy: anyone who steals the recipient's long-term X25519
key can open every message ever sealed to it. Forward secrecy would need a ratchet and is not part of this version.
Nothing here touches the network.
"""

import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from privatepartynet.vault import VaultError

IDENTITY_FILE = "identity.bin"
RAW = serialization.Encoding.Raw
PUBLIC_RAW = serialization.PublicFormat.Raw
PRIVATE_RAW = serialization.PrivateFormat.Raw
NO_ENCRYPTION = serialization.NoEncryption()


def _public_bytes(key):
    return key.public_bytes(RAW, PUBLIC_RAW)


def user_id_of(sign_public, dh_public):
    """The id for a pair of public keys (bytes or hex)."""
    sign_public, dh_public = _raw(sign_public), _raw(dh_public)
    return hashlib.sha256(b"PrivatePartyNet user" + sign_public + dh_public).digest()[:16].hex()


def _raw(value):
    return bytes.fromhex(value) if isinstance(value, str) else bytes(value)


def verify(sign_public, signature, data):
    """True when `signature` over `data` was made by the private key of `sign_public`."""
    try:
        Ed25519PublicKey.from_public_bytes(_raw(sign_public)).verify(_raw(signature), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def safety_number(card_a, card_b):
    """Twelve groups of five digits that two people read out to each other to confirm they hold each other's real keys.

    The same two cards give the same number on both PCs, whichever order they are passed in."""
    first, second = sorted([user_id_of(card_a["sign"], card_a["dh"]), user_id_of(card_b["sign"], card_b["dh"])])
    digest = hashlib.sha512(b"PrivatePartyNet safety" + bytes.fromhex(first) + bytes.fromhex(second)).digest()
    groups = [int.from_bytes(digest[i * 4:i * 4 + 4], "big") % 100000 for i in range(12)]
    return " ".join(f"{group:05d}" for group in groups)


class Identity:
    def __init__(self, sign_private, dh_private):
        self._sign = sign_private
        self._dh = dh_private
        self.sign_public = _public_bytes(sign_private.public_key())
        self.dh_public = _public_bytes(dh_private.public_key())
        self.user_id = user_id_of(self.sign_public, self.dh_public)

    # creation and storage
    @classmethod
    def create(cls):
        return cls(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, vault, folder):
        path = Path(folder) / IDENTITY_FILE
        if path.exists():
            return cls.from_private_bundle(vault.unseal("identity", path.read_bytes(), b"identity"))
        identity = cls.create()
        Path(folder).mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(vault.seal("identity", identity.private_bundle(), b"identity"))
        os.replace(temp, path)
        return identity

    def private_bundle(self):
        """The private keys as bytes, for the vault to seal or for an identity move to another PC."""
        return json.dumps({
            "sign": self._sign.private_bytes(RAW, PRIVATE_RAW, NO_ENCRYPTION).hex(),
            "dh": self._dh.private_bytes(RAW, PRIVATE_RAW, NO_ENCRYPTION).hex(),
        }).encode()

    @classmethod
    def from_private_bundle(cls, data):
        try:
            record = json.loads(data)
            return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(record["sign"])),
                       X25519PrivateKey.from_private_bytes(bytes.fromhex(record["dh"])))
        except (ValueError, KeyError, TypeError):
            raise VaultError("the identity file is damaged") from None

    # what others see
    def card(self, name=""):
        """The public half, as sent to others: names, keys and the id they hash to."""
        return {"id": self.user_id, "name": name, "sign": self.sign_public.hex(), "dh": self.dh_public.hex()}

    # signing
    def sign(self, data):
        return self._sign.sign(data)

    # a key two installs share
    def shared_key(self, peer_dh_public, info):
        """32 bytes that only this install and the holder of `peer_dh_public` can work out, the same on both PCs: X25519,
        then HKDF with `info` saying what the key is for (the paths between members use it to prove their probes)."""
        peer = _raw(peer_dh_public)
        shared = self._dh.exchange(X25519PublicKey.from_public_bytes(peer))
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"".join(sorted([self.dh_public, peer])),
                    info=b"PrivatePartyNet " + info).derive(shared)

    # sealing to a recipient
    def seal_to(self, recipient_dh_public, plaintext, aad=b""):
        """Encrypt `plaintext` so only the holder of `recipient_dh_public` can read it, and sign the result.

        Returns ephemeral public key (32) + nonce (12) + ciphertext + signature (64)."""
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(_raw(recipient_dh_public)))
        eph_public = _public_bytes(ephemeral.public_key())
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_public + _raw(recipient_dh_public),
                   info=b"PrivatePartyNet sealed").derive(shared)
        nonce = os.urandom(12)
        body = eph_public + nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad + self.sign_public)
        return body + self.sign(b"PrivatePartyNet sealed" + aad + body)

    def open_from(self, sender_sign_public, sealed, aad=b""):
        """The reverse of `seal_to`. Raises VaultError if the signature or the encryption does not check out."""
        if len(sealed) < 32 + 12 + 16 + 64:
            raise VaultError("sealed message is too short")
        body, signature = sealed[:-64], sealed[-64:]
        if not verify(sender_sign_public, signature, b"PrivatePartyNet sealed" + aad + body):
            raise VaultError("the signature does not match the sender")
        eph_public, nonce, ciphertext = body[:32], body[32:44], body[44:]
        shared = self._dh.exchange(X25519PublicKey.from_public_bytes(eph_public))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_public + self.dh_public,
                   info=b"PrivatePartyNet sealed").derive(shared)
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad + _raw(sender_sign_public))
        except InvalidTag:
            raise VaultError("the message could not be decrypted") from None
