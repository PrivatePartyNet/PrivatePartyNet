"""Spike 2: Windows DPAPI through ctypes, and per-row encryption speed in SQLite.

Checks that CryptProtectData/CryptUnprotectData round-trip with no extra package, that a different entropy value
fails to unprotect, and how long it takes to insert and page through 50,000 encrypted messages with ChaCha20-Poly1305.
Writes only to a temporary folder that it deletes.
"""
import ctypes
import os
import sqlite3
import tempfile
import time
from ctypes import wintypes

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect(data, entropy=b""):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, keep1 = _blob(data)
    extra, keep2 = _blob(entropy)
    out = Blob()
    if not crypt32.CryptProtectData(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def unprotect(data, entropy=b""):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, keep1 = _blob(data)
    extra, keep2 = _blob(entropy)
    out = Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def main():
    key = os.urandom(32)
    wrapped = protect(key, b"privatepartynet-master")
    assert unprotect(wrapped, b"privatepartynet-master") == key
    try:
        unprotect(wrapped, b"other")
        print("DPAPI: WRONG entropy was accepted")
    except OSError:
        print("DPAPI: round trip ok, wrong entropy refused; wrapped size", len(wrapped))

    aead = ChaCha20Poly1305(key)
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "chats.db")
    db = sqlite3.connect(path)
    db.execute("create table m(id integer primary key, channel text, ts real, body blob)")
    db.execute("create index m_channel on m(channel, id)")
    start = time.perf_counter()
    rows = []
    for i in range(50000):
        nonce = os.urandom(12)
        text = f"message number {i} with some ordinary chat text in it, about eighty characters long"
        rows.append((i, "general", time.time(), nonce + aead.encrypt(nonce, text.encode(), b"general")))
    db.executemany("insert into m values(?,?,?,?)", rows)
    db.commit()
    print(f"insert 50,000 encrypted rows: {time.perf_counter() - start:.2f} s")
    start = time.perf_counter()
    page = db.execute("select body from m where channel='general' order by id desc limit 200").fetchall()
    plain = [aead.decrypt(b[:12], b[12:], b"general") for (b,) in page]
    print(f"read and decrypt the newest 200: {(time.perf_counter() - start) * 1000:.1f} ms ({len(plain)} rows)")
    start = time.perf_counter()
    count = 0
    for (b,) in db.execute("select body from m"):
        aead.decrypt(b[:12], b[12:], b"general")
        count += 1
    print(f"decrypt all {count}: {time.perf_counter() - start:.2f} s")
    raw = open(path, "rb").read()
    print("plaintext visible in file:", b"ordinary chat text" in raw, "| file size", len(raw) // 1024, "KB")
    db.close()
    os.remove(path)
    os.rmdir(folder)


if __name__ == "__main__":
    main()
