"""Signs a release, so installed copies of PrivatePartyNet will update to it (privatepartynet/updater.py).

    py tools\\sign_release.py --new-key      make the signing key, once (refuses to replace one)
    py tools\\sign_release.py --public       print the public key, which privatepartynet/updater.py carries
    py tools\\sign_release.py                sign this version's release in dist\\ (build.py runs it)
    py tools\\sign_release.py --next-key HEX also hand installed copies the next key (key rotation)
    py tools\\sign_release.py --export PATH  write a backup of the key, sealed with a passphrase you type
    py tools\\sign_release.py --import PATH  seal a backup into this PC's key folder (refuses to replace a key)

The key never goes in the repository. It lives in %APPDATA%\\PrivatePartyNet-release\\update-signing.key, sealed with
Windows data protection for this user on this PC, so it cannot be copied to another PC or user as it is. Back it up
with --export, once, onto something kept off this PC (a USB stick in a drawer, a password manager's file storage):
without it no installed copy can be updated again, except by a new install from the website. The backup is sealed
with ChaCha20-Poly1305 under a key made from the passphrase (scrypt, 128 MiB), which is asked for at the prompt and
never taken from the command line or the environment. Keep the passphrase apart from the file.

Signing writes two files next to the installer and the portable zip:
    PrivatePartyNet-<version>-update.json      the version, its tag, and each file's name, size, SHA-256 and kind
    PrivatePartyNet-<version>-update.json.sig  the Ed25519 signature of that file, in hex
Upload both with the release, beside the installer and the zip.
"""

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: E402

from privatepartynet import __version__  # noqa: E402
from privatepartynet import updater  # noqa: E402
from privatepartynet.vault import dpapi_protect, dpapi_unprotect  # noqa: E402

KEY_FOLDER = Path(os.environ.get("APPDATA", str(Path.home()))) / "PrivatePartyNet-release"
KEY_FILE = KEY_FOLDER / "update-signing.key"
ENTROPY = b"PrivatePartyNet release signing key"
BACKUP_FORMAT = "PrivatePartyNet release signing key backup"
EXPORT_LOG2_N = 17  # scrypt's cost: 2^17 x 128 x 8 bytes = 128 MiB and about a second, for each passphrase tried
MIN_PASSPHRASE = 12


def load_key(path=KEY_FILE):
    if not path.is_file():
        raise SystemExit(f"No signing key at {path}. Make one with: py tools\\sign_release.py --new-key")
    return Ed25519PrivateKey.from_private_bytes(dpapi_unprotect(path.read_bytes(), ENTROPY))


def new_key(path=KEY_FILE):
    if path.exists():
        raise SystemExit(f"A signing key already exists at {path}; it is not replaced. Back it up instead.")
    key = Ed25519PrivateKey.generate()
    store_key(key, path)
    return key


def store_key(key, path=KEY_FILE):
    """Seal `key` with Windows data protection at `path`. Never replaces a key that is there."""
    if path.exists():
        raise SystemExit(f"A signing key already exists at {path}; it is not replaced. Back it up instead.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dpapi_protect(key.private_bytes_raw(), ENTROPY))


# --- the offline backup --------------------------------------------------------------------------------------


def _backup_key(passphrase, salt, log2_n, r, p):
    return Scrypt(salt=salt, length=32, n=2 ** log2_n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _covered(record):
    """What the seal's tag covers besides the key: every field but the sealed bytes, so none can be changed."""
    return json.dumps({k: v for k, v in record.items() if k != "sealed"}, sort_keys=True, separators=(",", ":")).encode()


def export_key(key, path, passphrase, log2_n=EXPORT_LOG2_N):
    """Write `key` to `path` (a new file) sealed under `passphrase`. The public key stays readable, to tell backups
    apart."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise SystemExit(f"Use a passphrase of at least {MIN_PASSPHRASE} characters; a few unrelated words are best.")
    record = {"format": BACKUP_FORMAT, "version": 1, "public": public_hex(key), "kdf": "scrypt", "log2_n": log2_n,
              "r": 8, "p": 1, "salt": os.urandom(16).hex(), "nonce": os.urandom(12).hex()}
    sealed = ChaCha20Poly1305(_backup_key(passphrase, bytes.fromhex(record["salt"]), log2_n, 8, 1)).encrypt(
        bytes.fromhex(record["nonce"]), key.private_bytes_raw(), _covered(record))
    record["sealed"] = sealed.hex()
    try:
        with open(path, "x", encoding="utf-8") as file:
            file.write(json.dumps(record, indent=2) + "\n")
    except FileExistsError:
        raise SystemExit(f"{path} already exists; it is not replaced. Pick another name.") from None


def import_key(path, passphrase):
    """The key in the backup at `path`, opened with `passphrase`. SystemExit, in plain words, when it cannot be."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("format") != BACKUP_FORMAT:
            raise SystemExit(f"{path} is not a PrivatePartyNet signing key backup.")
        if record.get("version") != 1:
            raise SystemExit(f"{path} was made by a newer sign_release.py.")
        log2_n, r, p = record["log2_n"], record["r"], record["p"]
        if record.get("kdf") != "scrypt" or not all(isinstance(v, int) for v in (log2_n, r, p)) \
                or not (10 <= log2_n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
            raise SystemExit(f"{path} is damaged.")
        salt, nonce, sealed = (bytes.fromhex(record[name]) for name in ("salt", "nonce", "sealed"))
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(f"{path} cannot be read, or is damaged.") from None
    try:
        raw = ChaCha20Poly1305(_backup_key(passphrase, salt, log2_n, r, p)).decrypt(nonce, sealed, _covered(record))
        key = Ed25519PrivateKey.from_private_bytes(raw)
    except (InvalidTag, ValueError):
        raise SystemExit("The passphrase is wrong, or the backup file is damaged.") from None
    if public_hex(key) != record.get("public"):
        raise SystemExit(f"{path} is damaged: its key is not the public key it names.")
    return key


def public_hex(key):
    return key.public_key().public_bytes_raw().hex()


def manifest_for(version, dist, notes="", next_key=None, key=None):
    """The manifest (bytes) for `version`'s installer and portable zip in `dist`."""
    files = []
    for name, kind in ((f"PrivatePartyNetSetup-{version}.exe", "installer"),
                       (f"PrivatePartyNet-{version}-portable.zip", "portable")):
        path = Path(dist) / name
        if not path.is_file():
            raise SystemExit(f"{path} is missing: build the release first.")
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
        files.append({"name": name, "size": path.stat().st_size, "sha256": digest.hexdigest(), "kind": kind})
    record = {"app": updater.APP, "version": version, "tag": "v" + version, "files": files}
    if notes:
        record["notes"] = notes
    if next_key:
        record["next_key"] = {"key": next_key, "sig": key.sign(updater.NEXT_KEY_LABEL + bytes.fromhex(next_key)).hex()}
    return (json.dumps(record, indent=2) + "\n").encode("utf-8")


def sign(version=__version__, dist=ROOT / "dist", key=None, next_key=None):
    """Write the manifest and its signature into `dist`. Returns their paths."""
    key = key or load_key()
    if public_hex(key) not in updater.TRUSTED_KEYS:
        print("  WARNING: this signing key is not one the app trusts (privatepartynet/updater.py TRUSTED_KEYS); "
              "installed copies will refuse this release.")
    data = manifest_for(version, dist, next_key=next_key, key=key)
    manifest = Path(dist) / updater.manifest_name(version)
    manifest.write_bytes(data)
    signature = manifest.with_name(manifest.name + ".sig")
    signature.write_text(key.sign(updater.MANIFEST_LABEL + data).hex() + "\n", encoding="ascii")
    return manifest, signature


def parser():
    parser = argparse.ArgumentParser(description="Sign a PrivatePartyNet release for the updater")
    parser.add_argument("--new-key", action="store_true", help="make the signing key (once)")
    parser.add_argument("--public", action="store_true", help="print the public key")
    parser.add_argument("--next-key", help="a new public key (hex) to hand to installed copies, signed by this one")
    parser.add_argument("--export", metavar="PATH", help="write a passphrase-sealed backup of the key to PATH")
    parser.add_argument("--import", dest="import_from", metavar="PATH", help="seal the backup at PATH into this PC")
    parser.add_argument("--version", default=__version__)
    return parser


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.export:
        key = load_key()
        passphrase = getpass.getpass("A passphrase for the backup (it is not shown): ")
        if getpass.getpass("The same passphrase again: ") != passphrase:
            raise SystemExit("The two passphrases differ; nothing was written.")
        export_key(key, Path(arguments.export), passphrase)
        print(f"Wrote {arguments.export}, the key {public_hex(key)} sealed with your passphrase.")
        print("Keep it off this PC, and the passphrase apart from it. To use it on a PC: --import that file.")
        return 0
    if arguments.import_from:
        key = import_key(arguments.import_from, getpass.getpass("The backup's passphrase: "))
        store_key(key)
        print(f"Sealed the key {public_hex(key)} into {KEY_FILE} for this Windows user.")
        if public_hex(key) not in updater.TRUSTED_KEYS:
            print("  WARNING: this key is not one the app trusts (privatepartynet/updater.py TRUSTED_KEYS).")
        return 0
    if arguments.new_key:
        key = new_key()
        print(f"Made {KEY_FILE}. Back it up. Its public key, for privatepartynet/updater.py TRUSTED_KEYS:")
        print(public_hex(key))
        return 0
    if arguments.public:
        print(public_hex(load_key()))
        return 0
    manifest, signature = sign(arguments.version, next_key=arguments.next_key)
    print(f"  {manifest.name}  sha256 {hashlib.sha256(manifest.read_bytes()).hexdigest()}")
    print(f"  {signature.name}  sha256 {hashlib.sha256(signature.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
