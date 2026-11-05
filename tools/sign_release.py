"""Signs a release, so installed copies of PrivatePartyNet will update to it (privatepartynet/updater.py).

    py tools\\sign_release.py --new-key      make the signing key, once (refuses to replace one)
    py tools\\sign_release.py --public       print the public key, which privatepartynet/updater.py carries
    py tools\\sign_release.py                sign this version's release in dist\\ (build.py runs it)
    py tools\\sign_release.py --next-key HEX also hand installed copies the next key (key rotation)

The key never goes in the repository. It lives in %APPDATA%\\PrivatePartyNet-release\\update-signing.key, sealed with
Windows data protection for this user on this PC. Back it up somewhere safe (copy the folder while signed in as the
same user; for another PC, export it with --export first): without it no installed copy can be updated again, except
by a new install from the website.

Signing writes two files next to the installer and the portable zip:
    PrivatePartyNet-<version>-update.json      the version, its tag, and each file's name, size, SHA-256 and kind
    PrivatePartyNet-<version>-update.json.sig  the Ed25519 signature of that file, in hex
Upload both with the release, beside the installer and the zip.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from privatepartynet import __version__  # noqa: E402
from privatepartynet import updater  # noqa: E402
from privatepartynet.vault import dpapi_protect, dpapi_unprotect  # noqa: E402

KEY_FOLDER = Path(os.environ.get("APPDATA", str(Path.home()))) / "PrivatePartyNet-release"
KEY_FILE = KEY_FOLDER / "update-signing.key"
ENTROPY = b"PrivatePartyNet release signing key"


def load_key(path=KEY_FILE):
    if not path.is_file():
        raise SystemExit(f"No signing key at {path}. Make one with: py tools\\sign_release.py --new-key")
    return Ed25519PrivateKey.from_private_bytes(dpapi_unprotect(path.read_bytes(), ENTROPY))


def new_key(path=KEY_FILE):
    if path.exists():
        raise SystemExit(f"A signing key already exists at {path}; it is not replaced. Back it up instead.")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dpapi_protect(key.private_bytes_raw(), ENTROPY))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sign a PrivatePartyNet release for the updater")
    parser.add_argument("--new-key", action="store_true", help="make the signing key (once)")
    parser.add_argument("--public", action="store_true", help="print the public key")
    parser.add_argument("--next-key", help="a new public key (hex) to hand to installed copies, signed by this one")
    parser.add_argument("--version", default=__version__)
    arguments = parser.parse_args(argv)
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
