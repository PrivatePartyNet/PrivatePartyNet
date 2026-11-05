"""The encrypted chat store: one SQLite file, a folder of image files and a folder of other files, all sealed with
subkeys of the vault.

Plain columns: ids, scope (which channel or conversation), sequence number, time, sender id. These let history page
quickly. Encrypted per row: the body (text, display name, attachment list). Each row's encryption is bound to its
scope and id, so a row copied into another conversation fails to open.

A scope is a text label: `srv:<server_id>:<channel_id>` for a channel and `dm:<user_id>` for a conversation with one
person. Messages are idempotent by id, so merging a backup or a re-sent message adds nothing twice.

A file of any kind is written as it arrives, never whole in memory: FILE_CHUNK bytes at a time, each piece sealed on its
own with its file id, its place and whether it is the last bound in, so pieces cannot be swapped, reordered or cut off.
Every sealed piece is SEAL_OVERHEAD bytes longer than its plain bytes, which lets a download start at any byte.

`held` is what a server this PC hosts keeps for its members (pictures and files), with their sizes, so the server can show
how much it keeps and keep to the owner's storage cap. A picture can be in several messages; `uses` counts them.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from privatepartynet.vault import VaultError

DB_FILE = "chats.db"
BLOB_DIR = "blobs"
FILE_DIR = "files"
FILE_CHUNK = 1024 * 1024
SEAL_OVERHEAD = 28  # vault.seal puts a 12-byte nonce before and a 16-byte tag after each piece
SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists messages(
  id text primary key, scope text not null, seq integer, ts real not null, sender text not null,
  body blob not null, sig blob);
create index if not exists messages_scope_ts on messages(scope, ts, id);
create index if not exists messages_scope_seq on messages(scope, seq);
create table if not exists contacts(user_id text primary key, card blob not null, added real not null);
create table if not exists servers(server_id text primary key, record blob not null, updated real not null);
create table if not exists outbox(id text primary key, scope text not null, peer text not null, payload blob not null,
  queued real not null, tries integer not null default 0, next_try real not null default 0);
create table if not exists reads(scope text primary key, last_ts real not null);
create table if not exists blobs(hash text primary key, size integer not null, added real not null);
create table if not exists files(id text primary key, size integer not null, sha256 text not null, added real not null);
create table if not exists held(server_id text not null, kind text not null, ref text not null, size integer not null,
  uses integer not null default 0, added real not null, primary key(server_id, kind, ref));
"""


def blob_hash(data):
    return hashlib.sha256(data).hexdigest()


def _chunk_aad(file_id, index, last):
    return f"file|{file_id}|{index}|{'last' if last else 'more'}".encode("utf-8")


def valid_file_id(file_id):
    return isinstance(file_id, str) and len(file_id) == 32 and all(c in "0123456789abcdef" for c in file_id)


class FileWriter:
    """One file going into the store as it arrives. write() as often as needed, then finish() for (id, size, sha256), or
    abort() to leave nothing behind. Nothing is visible to readers until finish()."""

    def __init__(self, store):
        self._store = store
        self.id = secrets.token_hex(16)
        self._path = store._file_path(self.id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._temp = self._path.with_suffix(".part")
        self._out = open(self._temp, "wb")
        self._buffer = bytearray()
        self._index = 0
        self.size = 0
        self._hash = hashlib.sha256()

    def write(self, data):
        self._hash.update(data)
        self.size += len(data)
        self._buffer += data
        # keep the last piece back until finish(), which is when it is known to be the last
        while len(self._buffer) > FILE_CHUNK:
            self._seal(bytes(self._buffer[:FILE_CHUNK]), last=False)
            del self._buffer[:FILE_CHUNK]

    def _seal(self, piece, last):
        self._out.write(self._store._vault.seal("files", piece, _chunk_aad(self.id, self._index, last)))
        self._index += 1

    def finish(self):
        if self.size == 0:
            self.abort()
            raise ValueError("an empty file")
        self._seal(bytes(self._buffer), last=True)
        self._buffer = bytearray()
        self._out.close()
        os.replace(self._temp, self._path)
        digest = self._hash.hexdigest()
        with self._store._lock:
            self._store._db.execute("insert into files(id, size, sha256, added) values(?,?,?,?)",
                                    (self.id, self.size, digest, time.time()))
            self._store._db.commit()
        return self.id, self.size, digest

    def abort(self):
        try:
            self._out.close()
        finally:
            try:
                self._temp.unlink()
            except FileNotFoundError:
                pass


class Store:
    def __init__(self, vault, folder):
        self._vault = vault
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / BLOB_DIR).mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.folder / DB_FILE), check_same_thread=False)
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # --- sealing -------------------------------------------------------------------------------------------

    def _seal(self, obj, aad):
        return self._vault.seal("store", json.dumps(obj, separators=(",", ":")).encode("utf-8"), aad)

    def _open(self, sealed, aad):
        return json.loads(self._vault.unseal("store", bytes(sealed), aad).decode("utf-8"))

    @staticmethod
    def _message_aad(scope, message_id):
        return f"msg|{scope}|{message_id}".encode("utf-8")

    # --- messages ------------------------------------------------------------------------------------------

    def add_message(self, message_id, scope, sender, body, ts=None, seq=None, sig=None):
        """Save a message. Returns True when it was new, False when that id was already there."""
        ts = time.time() if ts is None else ts
        sealed = self._seal(body, self._message_aad(scope, message_id))
        with self._lock:
            cursor = self._db.execute(
                "insert or ignore into messages(id, scope, seq, ts, sender, body, sig) values(?,?,?,?,?,?,?)",
                (message_id, scope, seq, ts, sender, sealed, sig))
            self._db.commit()
            return cursor.rowcount == 1

    def update_body(self, message_id, scope, changes):
        """Add or change fields of a stored message's body (for a mark such as "refused"). False when it is not there."""
        with self._lock:
            row = self._db.execute("select body from messages where id=? and scope=?", (message_id, scope)).fetchone()
            if row is None:
                return False
            body = self._open(row[0], self._message_aad(scope, message_id))
            body.update(changes)
            self._db.execute("update messages set body=? where id=? and scope=?",
                             (self._seal(body, self._message_aad(scope, message_id)), message_id, scope))
            self._db.commit()
            return True

    def history(self, scope, limit=100, before_ts=None):
        """The newest `limit` messages of a scope, oldest first. `before_ts` pages further back."""
        query = "select id, seq, ts, sender, body, sig from messages where scope=?"
        args = [scope]
        if before_ts is not None:
            query += " and ts<?"
            args.append(before_ts)
        query += " order by ts desc, coalesce(seq, 0) desc, rowid desc limit ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [self._row(scope, row) for row in reversed(rows)]

    def since_seq(self, scope, seq, limit=500):
        """Messages after sequence number `seq`, oldest first (what a rejoining member is missing)."""
        with self._lock:
            rows = self._db.execute(
                "select id, seq, ts, sender, body, sig from messages where scope=? and seq>? order by seq limit ?",
                (scope, int(seq), int(limit))).fetchall()
        return [self._row(scope, row) for row in rows]

    def last_seq(self, scope):
        with self._lock:
            row = self._db.execute("select max(seq) from messages where scope=?", (scope,)).fetchone()
        return row[0] or 0

    def count(self, scope=None):
        with self._lock:
            if scope is None:
                return self._db.execute("select count(*) from messages").fetchone()[0]
            return self._db.execute("select count(*) from messages where scope=?", (scope,)).fetchone()[0]

    def scopes(self):
        with self._lock:
            return [row[0] for row in self._db.execute("select distinct scope from messages order by scope")]

    def has_message(self, message_id):
        with self._lock:
            return self._db.execute("select 1 from messages where id=?", (message_id,)).fetchone() is not None

    def all_messages(self, scope):
        """Every message of a scope, oldest first, for export."""
        with self._lock:
            rows = self._db.execute(
                "select id, seq, ts, sender, body, sig from messages where scope=? order by ts, coalesce(seq, 0), rowid", (scope,)).fetchall()
        return [self._row(scope, row) for row in rows]

    def _row(self, scope, row):
        message_id, seq, ts, sender, body, sig = row
        return {"id": message_id, "scope": scope, "seq": seq, "ts": ts, "sender": sender,
                "body": self._open(body, self._message_aad(scope, message_id)),
                "sig": bytes(sig) if sig is not None else None}

    # --- read markers --------------------------------------------------------------------------------------

    def mark_read(self, scope, ts=None):
        with self._lock:
            self._db.execute("insert or replace into reads(scope, last_ts) values(?,?)",
                             (scope, time.time() if ts is None else ts))
            self._db.commit()

    def unread(self, scope, not_from=None):
        """Messages after the read marker. Pass `not_from` (your own id) to leave out what you wrote yourself."""
        with self._lock:
            row = self._db.execute("select last_ts from reads where scope=?", (scope,)).fetchone()
            since = row[0] if row else 0
            query, args = "select count(*) from messages where scope=? and ts>?", [scope, since]
            if not_from is not None:
                query += " and sender!=?"
                args.append(not_from)
            return self._db.execute(query, args).fetchone()[0]

    # --- contacts and servers ------------------------------------------------------------------------------

    def save_contact(self, card):
        """Remember someone's public card. A different key for a known id is refused: keys are pinned on first sight."""
        user_id = card["id"]
        with self._lock:
            existing = self.contact(user_id)
            if existing is not None and (existing["sign"], existing["dh"]) != (card["sign"], card["dh"]):
                raise VaultError("that id is already pinned to different keys")
            if existing is not None:
                merged = dict(existing)  # the pinned keys stay; everything else (name, address, port, flags) is updated
                merged.update({key: value for key, value in card.items() if key not in ("id", "sign", "dh")})
                card = merged
            self._db.execute("insert or replace into contacts(user_id, card, added) values(?,?,?)",
                             (user_id, self._seal(card, b"contact|" + user_id.encode()), time.time()))
            self._db.commit()

    def contact(self, user_id):
        with self._lock:
            row = self._db.execute("select card from contacts where user_id=?", (user_id,)).fetchone()
        return self._open(row[0], b"contact|" + user_id.encode()) if row else None

    def contacts(self):
        with self._lock:
            rows = self._db.execute("select user_id, card from contacts order by added").fetchall()
        return [self._open(card, b"contact|" + user_id.encode()) for user_id, card in rows]

    def save_server(self, server_id, record):
        with self._lock:
            self._db.execute("insert or replace into servers(server_id, record, updated) values(?,?,?)",
                             (server_id, self._seal(record, b"server|" + server_id.encode()), time.time()))
            self._db.commit()

    def server(self, server_id):
        with self._lock:
            row = self._db.execute("select record from servers where server_id=?", (server_id,)).fetchone()
        return self._open(row[0], b"server|" + server_id.encode()) if row else None

    def servers(self):
        with self._lock:
            rows = self._db.execute("select server_id, record from servers order by updated desc").fetchall()
        return [self._open(record, b"server|" + server_id.encode()) for server_id, record in rows]

    def server_items(self):
        """Every saved server as {"server_id", "record"}, for export."""
        with self._lock:
            rows = self._db.execute("select server_id, record from servers order by updated desc").fetchall()
        return [{"server_id": server_id, "record": self._open(record, b"server|" + server_id.encode())}
                for server_id, record in rows]

    def forget_server(self, server_id):
        with self._lock:
            self._db.execute("delete from servers where server_id=?", (server_id,))
            self._db.commit()

    # --- outbox --------------------------------------------------------------------------------------------

    def queue_outgoing(self, message_id, scope, peer, payload):
        with self._lock:
            self._db.execute(
                "insert or ignore into outbox(id, scope, peer, payload, queued) values(?,?,?,?,?)",
                (message_id, scope, peer, self._seal(payload, b"outbox|" + message_id.encode()), time.time()))
            self._db.commit()

    def due_outgoing(self, now=None, peer=None):
        now = time.time() if now is None else now
        query = "select id, scope, peer, payload, tries from outbox where next_try<=?"
        args = [now]
        if peer is not None:
            query += " and peer=?"
            args.append(peer)
        with self._lock:
            rows = self._db.execute(query + " order by queued", args).fetchall()
        return [{"id": i, "scope": s, "peer": p, "payload": self._open(b, b"outbox|" + i.encode()), "tries": t}
                for i, s, p, b, t in rows]

    def sent(self, message_id):
        with self._lock:
            self._db.execute("delete from outbox where id=?", (message_id,))
            self._db.commit()

    def failed(self, message_id, now=None):
        """Back off: 5 s, 10 s, 20 s and so on, up to 15 minutes between tries."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute("select tries from outbox where id=?", (message_id,)).fetchone()
            if row is None:
                return
            tries = row[0] + 1
            self._db.execute("update outbox set tries=?, next_try=? where id=?",
                             (tries, now + min(900, 5 * 2 ** (tries - 1)), message_id))
            self._db.commit()

    def outbox_count(self):
        with self._lock:
            return self._db.execute("select count(*) from outbox").fetchone()[0]

    # --- image and file blobs ------------------------------------------------------------------------------

    def _blob_path(self, digest):
        return self.folder / BLOB_DIR / digest[:2] / digest

    def put_blob(self, data):
        """Store bytes sealed on disk, named by their SHA-256. Returns the hash."""
        digest = blob_hash(data)
        path = self._blob_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_bytes(self._vault.seal("blobs", data, b"blob|" + digest.encode()))
            os.replace(temp, path)
            with self._lock:
                self._db.execute("insert or ignore into blobs(hash, size, added) values(?,?,?)",
                                 (digest, len(data), time.time()))
                self._db.commit()
        return digest

    def get_blob(self, digest):
        path = self._blob_path(digest)
        if not path.exists():
            return None
        data = self._vault.unseal("blobs", path.read_bytes(), b"blob|" + digest.encode())
        if blob_hash(data) != digest:
            raise VaultError("a stored image does not match its name")
        return data

    def has_blob(self, digest):
        return self._blob_path(digest).exists()

    def blob_size(self, digest):
        with self._lock:
            row = self._db.execute("select size from blobs where hash=?", (digest,)).fetchone()
        return row[0] if row else None

    def delete_blob(self, digest):
        with self._lock:
            self._db.execute("delete from blobs where hash=?", (digest,))
            self._db.commit()
        try:
            self._blob_path(digest).unlink()
        except FileNotFoundError:
            pass

    # --- files of any kind, kept in pieces ------------------------------------------------------------------

    def _file_path(self, file_id):
        return self.folder / FILE_DIR / file_id[:2] / file_id

    def begin_file(self):
        return FileWriter(self)

    def file_size(self, file_id):
        if not valid_file_id(file_id):
            return None
        with self._lock:
            row = self._db.execute("select size from files where id=?", (file_id,)).fetchone()
        return row[0] if row else None

    def read_file(self, file_id, start=0, end=None):
        """The plain bytes of a stored file from `start` up to, not including, `end` (the whole file by default), as
        pieces. Raises VaultError when a piece has been changed, moved or cut off, and KeyError for an unknown file."""
        size = self.file_size(file_id)
        if size is None:
            raise KeyError(file_id)
        end = size if end is None else min(end, size)
        return self._pieces(file_id, size, max(0, start), end)

    def _pieces(self, file_id, size, start, end):
        if start >= end:
            return
        count = (size + FILE_CHUNK - 1) // FILE_CHUNK
        with open(self._file_path(file_id), "rb") as source:
            index = start // FILE_CHUNK
            while index * FILE_CHUNK < end:
                last = index == count - 1
                length = (size - index * FILE_CHUNK if last else FILE_CHUNK) + SEAL_OVERHEAD
                source.seek(index * (FILE_CHUNK + SEAL_OVERHEAD))
                sealed = source.read(length)
                if len(sealed) != length:
                    raise VaultError("a stored file has been cut short")
                piece = self._vault.unseal("files", sealed, _chunk_aad(file_id, index, last))
                low = max(start - index * FILE_CHUNK, 0)
                high = min(end - index * FILE_CHUNK, len(piece))
                yield piece[low:high]
                index += 1

    def delete_file(self, file_id):
        if not valid_file_id(file_id):
            return
        with self._lock:
            self._db.execute("delete from files where id=?", (file_id,))
            self._db.commit()
        try:
            self._file_path(file_id).unlink()
        except FileNotFoundError:
            pass

    # --- what a hosted server keeps ------------------------------------------------------------------------

    def hold(self, server_id, kind, ref, size, uses=0):
        """Count a picture ("image", by hash) or a file ("file", by id) as kept by a server. Adding one already counted
        changes nothing but `uses`, which goes up by `uses`."""
        with self._lock:
            self._db.execute("insert or ignore into held(server_id, kind, ref, size, uses, added) values(?,?,?,?,0,?)",
                             (server_id, kind, ref, int(size), time.time()))
            if uses:
                self._db.execute("update held set uses=uses+? where server_id=? and kind=? and ref=?",
                                 (uses, server_id, kind, ref))
            self._db.commit()

    def release(self, server_id, kind, ref):
        """One message using a kept picture or file is gone. Returns True when nothing uses it any more; it is then no
        longer counted (the caller deletes the bytes)."""
        with self._lock:
            row = self._db.execute("select uses from held where server_id=? and kind=? and ref=?",
                                   (server_id, kind, ref)).fetchone()
            if row is None:
                return False
            if row[0] > 1:
                self._db.execute("update held set uses=uses-1 where server_id=? and kind=? and ref=?",
                                 (server_id, kind, ref))
                self._db.commit()
                return False
            self._db.execute("delete from held where server_id=? and kind=? and ref=?", (server_id, kind, ref))
            self._db.commit()
            return True

    def held_bytes(self, server_id):
        with self._lock:
            return self._db.execute("select coalesce(sum(size), 0) from held where server_id=?",
                                    (server_id,)).fetchone()[0]

    def largest_held(self, server_id, limit=10):
        """The biggest things a server keeps, as (kind, ref, size), biggest first."""
        with self._lock:
            return self._db.execute("select kind, ref, size from held where server_id=? order by size desc limit ?",
                                    (server_id, limit)).fetchall()

    # --- deleting a message --------------------------------------------------------------------------------

    def message(self, message_id, scope):
        """One stored message as the history gives it, or None."""
        with self._lock:
            row = self._db.execute("select id, seq, ts, sender, body, sig from messages where id=? and scope=?",
                                   (message_id, scope)).fetchone()
        return self._row(scope, row) if row else None

    def delete_message(self, message_id, scope):
        with self._lock:
            cursor = self._db.execute("delete from messages where id=? and scope=?", (message_id, scope))
            self._db.commit()
            return cursor.rowcount == 1

    def blob_hashes(self):
        with self._lock:
            return [row[0] for row in self._db.execute("select hash from blobs order by added")]
