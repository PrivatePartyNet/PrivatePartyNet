"""Export all chats to one file for safekeeping or a move to another PC, and import such a file.

The file is a zip (messages as JSON lines, deflated; pictures stored as they are, since they are compressed already)
sealed in 1 MiB chunks with ChaCha20-Poly1305 under a key made from a passphrase the person chooses (scrypt). Sealing
in chunks keeps memory flat for a large history. Each chunk's tag covers the header, the chunk's number and whether it
is the last one, so a wrong passphrase, a changed byte, a missing chunk and a cut-off file all fail cleanly.

Importing merges: a message that is already in the store (same id) is skipped, so importing twice, or into a PC that
already has part of the history, adds nothing twice. Contacts keep the keys they were first pinned with. The identity
(the keys that make a person who they are) travels only when the export says so, and is never applied automatically.
"""

import json
import os
import struct
import tempfile
import time
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from privatepartynet import __version__

MAGIC = b"PPN-BKUP"
FORMAT_VERSION = 1
CHUNK = 1024 * 1024
HEADER_SIZE = 8 + 1 + 16 + 1 + 1 + 1 + 4 + 4
MAX_LOG2_N = 18  # refuse a header that asks for more memory than this (a damaged or hostile file)
DEFAULT_LOG2_N = 15
MAX_ENTRY = 64 * 1024 * 1024  # one unpacked entry; a picture is at most 12 MB and a scope's messages are far below this
MAX_TOTAL = 16 * 1024 * 1024 * 1024
EXTENSION = ".ppnbak"


class BackupError(Exception):
    """The file is not a backup, is damaged, or the passphrase is wrong. The message says which, in plain words."""


def _key(passphrase, salt, log2_n, r=8, p=1):
    if not passphrase:
        raise BackupError("A passphrase is needed.")
    return Scrypt(salt=salt, length=32, n=2 ** log2_n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _nonce(prefix, counter):
    return prefix + struct.pack(">Q", counter)


# --- sealing and opening a stream ----------------------------------------------------------------------------


def seal_file(source_path, destination_path, passphrase, log2_n=DEFAULT_LOG2_N, progress=None):
    """Seal the file at `source_path` into `destination_path`."""
    salt, prefix = os.urandom(16), os.urandom(4)
    header = MAGIC + bytes([FORMAT_VERSION]) + salt + bytes([log2_n, 8, 1]) + prefix + struct.pack(">I", CHUNK)
    aead = ChaCha20Poly1305(_key(passphrase, salt, log2_n))
    size = max(1, os.path.getsize(source_path))
    done = 0
    with open(source_path, "rb") as source, open(destination_path, "wb") as out:
        out.write(header)
        counter = 0
        block = source.read(CHUNK)
        while True:
            following = source.read(CHUNK)
            final = not following
            flag = b"\x01" if final else b"\x00"
            sealed = aead.encrypt(_nonce(prefix, counter), block, header + flag + struct.pack(">Q", counter))
            out.write(flag + struct.pack(">I", len(sealed)) + sealed)
            done += len(block)
            counter += 1
            if progress:
                progress(done, size)
            if final:
                break
            block = following


def open_file(source_path, destination_path, passphrase, progress=None):
    """Open a sealed file into `destination_path`. Raises BackupError for a wrong passphrase or any damage."""
    total = max(1, os.path.getsize(source_path))
    with open(source_path, "rb") as source:
        header = source.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[:8] != MAGIC:
            raise BackupError("This is not a PrivatePartyNet backup file.")
        if header[8] != FORMAT_VERSION:
            raise BackupError("This backup was made by a newer PrivatePartyNet. Update PrivatePartyNet to open it.")
        salt, log2_n, r, p, prefix = header[9:25], header[25], header[26], header[27], header[28:32]
        (chunk_size,) = struct.unpack(">I", header[32:36])
        if log2_n > MAX_LOG2_N or r > 16 or p > 4 or chunk_size != CHUNK:
            raise BackupError("This backup file is damaged.")
        aead = ChaCha20Poly1305(_key(passphrase, salt, log2_n, r, p))
        counter = 0
        read = HEADER_SIZE
        with open(destination_path, "wb") as out:
            while True:
                head = source.read(5)
                if len(head) < 5:
                    raise BackupError("This backup file is cut short.")
                flag, (length,) = head[:1], struct.unpack(">I", head[1:5])
                if length > CHUNK + 16 or flag not in (b"\x00", b"\x01"):
                    raise BackupError("This backup file is damaged.")
                sealed = source.read(length)
                if len(sealed) < length:
                    raise BackupError("This backup file is cut short.")
                try:
                    block = aead.decrypt(_nonce(prefix, counter), sealed, header + flag + struct.pack(">Q", counter))
                except InvalidTag:
                    raise BackupError("That passphrase is wrong, or the file is damaged." if counter == 0
                                      else "This backup file is damaged.") from None
                out.write(block)
                counter += 1
                read += 5 + length
                if progress:
                    progress(min(read, total), total)
                if flag == b"\x01":
                    if source.read(1):
                        raise BackupError("This backup file has extra data after its end.")
                    return


# --- export ---------------------------------------------------------------------------------------------------


def export_all(store, passphrase, destination, identity=None, scopes=None, app_version=None, progress=None,
               log2_n=DEFAULT_LOG2_N):
    """Write every chat (or just `scopes`), the pictures they use, contacts and saved servers to `destination`.

    Pass `identity` to carry the person's keys too: anyone with the file and the passphrase can then BE that person, so
    the page that offers this says so. Returns a report: {path, scopes, messages, blobs, size, identity}."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    chosen = list(scopes) if scopes is not None else store.scopes()
    folder = tempfile.mkdtemp(prefix="privatepartynet-backup-", dir=str(destination.parent))
    plain = Path(folder) / "archive.zip"
    sealed = Path(folder) / "sealed.bin"
    try:
        wanted_blobs = set()
        index = []
        message_total = 0
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for number, scope in enumerate(chosen):
                name = f"messages/{number:05d}.jsonl"
                count = 0
                with archive.open(name, "w") as entry:
                    for message in store.all_messages(scope):
                        record = {"id": message["id"], "scope": scope, "seq": message["seq"], "ts": message["ts"],
                                  "sender": message["sender"], "body": message["body"],
                                  "sig": message["sig"].hex() if message["sig"] else None}
                        entry.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
                        image = message["body"].get("image")
                        if image and image.get("hash"):
                            wanted_blobs.add(image["hash"])
                        for attachment in message["body"].get("attachments") or []:
                            if isinstance(attachment, dict) and attachment.get("hash"):
                                wanted_blobs.add(attachment["hash"])
                        count += 1
                index.append({"scope": scope, "file": name, "count": count})
                message_total += count
                if progress:
                    progress(number + 1, len(chosen) + 2)
            blob_count = 0
            for digest in sorted(wanted_blobs):
                data = store.get_blob(digest)
                if data is None:
                    continue
                archive.writestr(zipfile.ZipInfo(f"blobs/{digest}"), data, compress_type=zipfile.ZIP_STORED)
                blob_count += 1
            archive.writestr("contacts.json", json.dumps(store.contacts()))
            archive.writestr("servers.json", json.dumps(store.server_items()))
            if identity is not None:
                archive.writestr("identity.json", identity.private_bundle().decode("utf-8"))
            archive.writestr("manifest.json", json.dumps({
                "format": "privatepartynet-backup", "version": FORMAT_VERSION, "app": f"PrivatePartyNet {app_version or __version__}",
                "created": time.time(), "scopes": index, "messages": message_total, "blobs": blob_count,
                "identity": identity is not None}))
        seal_file(plain, sealed, passphrase, log2_n=log2_n)
        os.replace(sealed, destination)
        return {"path": str(destination), "scopes": len(chosen), "messages": message_total, "blobs": blob_count,
                "size": destination.stat().st_size, "identity": identity is not None}
    finally:
        for leftover in Path(folder).glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            Path(folder).rmdir()
        except OSError:
            pass


# --- import ---------------------------------------------------------------------------------------------------


def _read_manifest(archive):
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("This file is not a PrivatePartyNet backup.") from None
    if manifest.get("format") != "privatepartynet-backup":
        raise BackupError("This file is not a PrivatePartyNet backup.")
    return manifest


def _check_archive(archive):
    total = 0
    for info in archive.infolist():
        name = info.filename
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise BackupError("This backup file is damaged.")
        if info.file_size > MAX_ENTRY:
            raise BackupError("This backup file is damaged.")
        total += info.file_size
        if total > MAX_TOTAL:
            raise BackupError("This backup file is damaged.")


def import_backup(store, source, passphrase, progress=None):
    """Merge a backup into the store. Returns a report of what was added and what was already there.

    {messages_added, messages_skipped, blobs_added, contacts_added, contacts_kept, servers_added, has_identity}"""
    folder = tempfile.mkdtemp(prefix="privatepartynet-import-")
    plain = Path(folder) / "archive.zip"
    report = {"messages_added": 0, "messages_skipped": 0, "blobs_added": 0, "contacts_added": 0, "contacts_kept": 0,
              "servers_added": 0, "has_identity": False}
    try:
        open_file(source, plain, passphrase, progress=progress)
        try:
            archive = zipfile.ZipFile(plain)
        except zipfile.BadZipFile:
            raise BackupError("This backup file is damaged.") from None
        with archive:
            _check_archive(archive)
            manifest = _read_manifest(archive)
            report["has_identity"] = "identity.json" in archive.namelist()
            for entry in manifest.get("scopes", []):
                name = entry.get("file", "")
                if not name.startswith("messages/") or name not in archive.namelist():
                    raise BackupError("This backup file is damaged.")
                with archive.open(name) as lines:
                    for raw in lines:
                        try:
                            record = json.loads(raw)
                            added = store.add_message(
                                record["id"], record["scope"], record["sender"], record["body"], ts=record["ts"],
                                seq=record.get("seq"), sig=bytes.fromhex(record["sig"]) if record.get("sig") else None)
                        except (ValueError, KeyError, TypeError):
                            raise BackupError("This backup file is damaged.") from None
                        report["messages_added" if added else "messages_skipped"] += 1
            for name in archive.namelist():
                if name.startswith("blobs/"):
                    digest = name[len("blobs/"):]
                    data = archive.read(name)
                    if store.put_blob(data) != digest:
                        raise BackupError("This backup file is damaged.")
                    report["blobs_added"] += 1
            for card in json.loads(archive.read("contacts.json")) if "contacts.json" in archive.namelist() else []:
                had = store.contact(card.get("id")) is not None
                try:
                    store.save_contact(card)
                except Exception:
                    report["contacts_kept"] += 1  # the PC already pins different keys to that id; the old ones stay
                    continue
                report["contacts_kept" if had else "contacts_added"] += 1
            for item in json.loads(archive.read("servers.json")) if "servers.json" in archive.namelist() else []:
                server_id = item.get("server_id")
                if isinstance(server_id, str) and server_id and store.server(server_id) is None:
                    store.save_server(server_id, item.get("record") or {})
                    report["servers_added"] += 1
        return report
    finally:
        for leftover in Path(folder).glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            Path(folder).rmdir()
        except OSError:
            pass


def read_identity(source, passphrase):
    """The private keys stored in a backup as bytes, or None when the export did not include them."""
    folder = tempfile.mkdtemp(prefix="privatepartynet-import-")
    plain = Path(folder) / "archive.zip"
    try:
        open_file(source, plain, passphrase)
        with zipfile.ZipFile(plain) as archive:
            if "identity.json" not in archive.namelist():
                return None
            return archive.read("identity.json")
    except zipfile.BadZipFile:
        raise BackupError("This backup file is damaged.") from None
    finally:
        for leftover in Path(folder).glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            Path(folder).rmdir()
        except OSError:
            pass
