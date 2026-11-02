"""The key that protects everything PrivatePartyNet stores about chats.

A random 32-byte master key is made on first use. By default Windows protects it for the signed-in user (DPAPI), so
there is no password to type. A password can be added: the master key is then wrapped with a key derived from the
password (scrypt) and Windows is no longer involved. Changing or removing the password re-wraps the master key only;
nothing stored with it is re-encrypted.

Everything else asks the vault for a subkey by label (`subkey("store")`, `subkey("identity")`), so one stolen subkey
does not open the others.

What this protects against: another Windows account, a copied disk, a lost laptop. What it cannot protect against:
malware running as the same user, who can ask Windows to unwrap the key exactly as this code does.
"""

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEY_FILE = "master.json"
ENTROPY = b"PrivatePartyNet vault v1"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}  # about 32 MB and a tenth of a second; the values are saved with the file


class VaultError(Exception):
    """The key file is damaged, or Windows cannot unwrap it (another account, another PC)."""


class Locked(VaultError):
    """The vault has a password and has not been unlocked yet."""


class WrongPassword(VaultError):
    pass


# --- Windows data protection ---------------------------------------------------------------------------------


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def dpapi_available():
    return sys.platform == "win32"


def dpapi_protect(data, entropy=ENTROPY):
    """Wrap `data` so only this Windows user on this PC can unwrap it."""
    if not dpapi_available():
        raise VaultError("Windows data protection is only available on Windows")
    source, keep_source = _blob(data)
    extra, keep_extra = _blob(entropy)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0,
                                                  ctypes.byref(out)):
        raise VaultError(f"CryptProtectData failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_unprotect(data, entropy=ENTROPY):
    if not dpapi_available():
        raise VaultError("Windows data protection is only available on Windows")
    source, keep_source = _blob(data)
    extra, keep_extra = _blob(entropy)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0,
                                                    ctypes.byref(out)):
        raise VaultError("Windows could not unwrap the key (it was made by another user or on another PC)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# --- sealing --------------------------------------------------------------------------------------------------


def seal(key, plaintext, aad=b""):
    """ChaCha20-Poly1305 with a fresh random nonce. Returns nonce + ciphertext + tag."""
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def unseal(key, sealed, aad=b""):
    if len(sealed) < 12 + 16:
        raise VaultError("sealed data is too short")
    try:
        return ChaCha20Poly1305(key).decrypt(sealed[:12], sealed[12:], aad)
    except InvalidTag:
        raise VaultError("sealed data failed its integrity check") from None


def derive(key, label, length=32):
    """A subkey for one purpose, from a key and a text label."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=b"PrivatePartyNet " + label.encode()).derive(key)


def password_key(password, salt, params=None):
    params = params or SCRYPT
    return Scrypt(salt=salt, length=32, n=params["n"], r=params["r"], p=params["p"]).derive(password.encode("utf-8"))


# --- the vault ------------------------------------------------------------------------------------------------


class Vault:
    """Opens, creates and re-wraps the master key in `folder`/master.json."""

    def __init__(self, folder, scrypt_params=None, use_dpapi=None):
        self.folder = Path(folder)
        self.path = self.folder / KEY_FILE
        self._params = scrypt_params or SCRYPT
        self._use_dpapi = dpapi_available() if use_dpapi is None else use_dpapi
        self._master = None
        self._record = None
        self._load()

    # state
    @property
    def has_password(self):
        return bool(self._record) and self._record["mode"] == "password"

    @property
    def unlocked(self):
        return self._master is not None

    def _load(self):
        if self.path.exists():
            try:
                self._record = json.loads(self.path.read_text(encoding="utf-8"))
                _ = self._record["mode"], self._record["wrapped"]
            except (ValueError, KeyError, OSError):
                raise VaultError("the key file is damaged") from None
            if self._record["mode"] == "dpapi":
                self._master = dpapi_unprotect(bytes.fromhex(self._record["wrapped"]))
                if len(self._master) != 32:
                    raise VaultError("the key file is damaged")
        else:
            self._master = os.urandom(32)
            self._record = self._wrap_default(self._master)
            self._write(self._record)

    def _wrap_default(self, master):
        if self._use_dpapi:
            return {"v": 1, "mode": "dpapi", "wrapped": dpapi_protect(master).hex()}
        # Without Windows (a test on another system) the key is stored beside the data. Never reached in the app.
        return {"v": 1, "mode": "plain", "wrapped": master.hex()}

    def _write(self, record):
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temp, self.path)

    # passwords
    def unlock(self, password=None):
        if self.unlocked:
            return
        if self._record["mode"] == "plain":
            self._master = bytes.fromhex(self._record["wrapped"])
            return
        if password is None:
            raise Locked("this vault needs its password")
        params = self._record.get("kdf") or SCRYPT
        key = password_key(password, bytes.fromhex(self._record["salt"]), params)
        try:
            self._master = unseal(key, bytes.fromhex(self._record["wrapped"]), b"master")
        except VaultError:
            raise WrongPassword("that is not the password") from None

    def set_password(self, new_password, old_password=None):
        """Add a password, or change it (then `old_password` is checked first)."""
        if not new_password:
            raise ValueError("an empty password is no password; use remove_password")
        self._require_unlocked(old_password)
        salt = os.urandom(16)
        key = password_key(new_password, salt, self._params)
        self._record = {"v": 1, "mode": "password", "salt": salt.hex(), "kdf": dict(self._params),
                        "wrapped": seal(key, self._master, b"master").hex()}
        self._write(self._record)

    def remove_password(self, password):
        """Go back to a key that Windows protects, with no password."""
        if not self.has_password:
            return
        self._require_unlocked(password)
        self._record = self._wrap_default(self._master)
        self._write(self._record)

    def _require_unlocked(self, password):
        if self.has_password:
            # always re-check: a changed password must be proven, even in an unlocked session
            params = self._record.get("kdf") or SCRYPT
            if password is None:
                raise Locked("the current password is needed")
            key = password_key(password, bytes.fromhex(self._record["salt"]), params)
            try:
                master = unseal(key, bytes.fromhex(self._record["wrapped"]), b"master")
            except VaultError:
                raise WrongPassword("that is not the password") from None
            self._master = master
        elif not self.unlocked:
            raise Locked("this vault needs its password")

    def lock(self):
        if self.has_password:
            self._master = None

    # use
    def subkey(self, label):
        if not self.unlocked:
            raise Locked("this vault needs its password")
        return derive(self._master, label)

    def seal(self, label, plaintext, aad=b""):
        return seal(self.subkey(label), plaintext, aad)

    def unseal(self, label, sealed, aad=b""):
        return unseal(self.subkey(label), sealed, aad)
