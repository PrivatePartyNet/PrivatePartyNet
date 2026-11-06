"""PrivatePartyNet's app: one native window with the rail of servers, Home, and each server's room in it.

The windows are WebView2 windows (pywebview), so there is no browser chrome. The controls are web pages in the
app's window and talk to this Python object through the bridge. Guests who join from another PC use their own
browser, because that is where their screen picker lives. Nothing here reaches the internet.
"""

import base64
import ctypes
import importlib.metadata
import json
import logging
import os
import platform
import queue
import re
import secrets
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlsplit

import webview

from privatepartynet import APP_NAME, __version__
from privatepartynet import backup, connect, gamenet, windowpins
from privatepartynet.gameclient import GameMesh, GameSession
from privatepartynet.netlink import NetLink
from privatepartynet.cert import ensure_certificate, pins_of_pem
from privatepartynet.discovery import DEFAULT_PORT as DISCOVERY_PORT
from privatepartynet.discovery import Discovery, clean_name
from privatepartynet.dm import DmService, scope_for
from privatepartynet.feed import FeedServer
from privatepartynet.frames import ScreenGrabber
from privatepartynet.identity import Identity, safety_number
from privatepartynet.netinfo import LOOPBACK, address_for, lan_addresses
from privatepartynet.paths import data_dir, web_dir
from privatepartynet.server import Forbidden, NotFound, Party, generate_password
from privatepartynet.shellserver import ShellServer
from privatepartynet.settings import (DENSITIES, SOUND_GROUPS, THEMES, load_settings, save_settings, valid_accent,
                                      valid_mbps, valid_port, valid_text_scale)
from privatepartynet.store import Store
from privatepartynet.updater import Updater
from privatepartynet.vault import Locked, Vault, VaultError, WrongPassword

LOG_FILE = "privatepartynet.log"
# The control panel's boxes that hide a message looking like a certificate, key or token, and what kinds it tells apart
# (web/safetext.js). Controller.hidden_message logs only these words.
_HIDING_BOXES = frozenset({"notice", "ask note", "backup note", "reach", "invite note", "join status", "game network",
                           "game network paths"})
_HIDDEN_KINDS = frozenset({"a certificate or key", "a code or token"})
DEFAULT_PORT = 8765
REQUEST_LIFETIME = 30.0
MUTEX_NAME = "Local\\PrivatePartyNet.HostWindow"
SHOW_EVENT = "Local\\PrivatePartyNet.ShowWindow"  # a second copy sets it; the first brings its window to the front
ERROR_ALREADY_EXISTS = 183

log = logging.getLogger("privatepartynet")
_mutex = None  # held for the life of the process, so a second copy can see it


def configure_logging(folder=None):
    """Log to privatepartynet.log in the data folder. The windowed app has no console to print to."""
    folder = Path(folder or data_dir())
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def acquire_single_instance():
    """True when this is the only running copy. Always True off Windows."""
    global _mutex
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def ask_first_copy_to_show():
    """A second copy was started: ask the running one to bring its window to the front, so starting PrivatePartyNet
    again shows the one window instead of a second one. The second copy may hand its right to take the foreground."""
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    user32.AllowSetForegroundWindow(ctypes.c_uint32(0xFFFFFFFF))  # ASFW_ANY
    handle = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if handle:
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)


def watch_for_second_copy(window, stop):
    """Bring `window` to the front each time a second copy asks (ask_first_copy_to_show), until `stop` is set. Waits in
    steps of a second, so it ends within a second of the app closing."""
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateEventW(None, False, False, SHOW_EVENT)  # auto-reset
    if not handle:
        return
    try:
        while not stop.is_set():
            if kernel32.WaitForSingleObject(handle, 1000) != 0:  # WAIT_OBJECT_0
                continue
            try:
                window.restore()
                window.show()
                window.on_top = True
                window.on_top = False
            except Exception:
                log.debug("The window could not be brought to the front", exc_info=True)
    finally:
        kernel32.CloseHandle(handle)


def report_crash(exc_type, exc, traceback):
    """Anything the app does not catch is logged, so it does not vanish silently."""
    log.error("Unhandled error", exc_info=(exc_type, exc, traceback))


CREDITS = (
    ("pywebview", "pywebview", "the native window around the web pages"),
    ("mss", "mss", "screen capture"),
    ("Pillow", "Pillow", "picture encoding"),
    ("cryptography", "cryptography", "each server's certificate"),
    ("psutil", "psutil", "the CPU readout"),
    ("pythonnet", "pythonnet", "the bridge to Windows"),
)


def webview2_version():
    """The installed WebView2 runtime version, from the same registry keys the installer checks. Empty if not found."""
    try:
        import winreg
    except ImportError:
        return ""
    key_path = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, key_path), (winreg.HKEY_CURRENT_USER, key_path.replace("WOW6432Node\\", ""))):
        try:
            with winreg.OpenKey(hive, path) as key:
                return str(winreg.QueryValueEx(key, "pv")[0])
        except OSError:
            continue
    return ""


def library_version(distribution):
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not found"


def webview_options(storage_path=None):
    """The arguments the window stack is started with.

    pywebview's default is private mode, and in private mode every window it creates deletes ALL cookies in the shared
    WebView2 profile when it starts (edgechromium.py: CookieManager.DeleteAllCookies). Opening a pop-out, or any other
    window, then signed every window that was already open out of its party: its next chat, share or pop-out request
    arrived with no login and was refused, and the page called that "that screen is not live". A normal profile in the
    app's data folder keeps the cookies, and also keeps the page's saved choices (receive size, pointer, side panel)
    between runs."""
    folder = Path(storage_path) if storage_path else data_dir() / "webview"
    folder.mkdir(parents=True, exist_ok=True)
    return {"debug": False, "private_mode": False, "storage_path": str(folder)}


def browser_arguments(current):
    """The WebView2 browser arguments: `current` (the variable may already be set on this PC, by a game launcher or as a
    WebView2 workaround) without --ignore-certificate-errors. That flag made every window accept any certificate at
    all; party windows accept the one their join checked instead (windowpins.py), and the flag would keep WebView2
    from ever asking."""
    return " ".join(arg for arg in (current or "").split() if arg != "--ignore-certificate-errors")


HEX = re.compile(r"^[0-9a-f]+$")
KEEP_BATCH = 500  # messages one keep_chat call may hand over
KEEP_BLOBS = 20  # pictures one call may ask for


class WindowApi:
    """The calls a server's room may make into the app. The room is a frame of the one window; the window's own page
    (appshell.js) passes on only these calls (ROOM_CALLS), only from that room's frame, keyed by the server. Names that
    start with an underscore are not offered. There are no pop-out windows: PrivatePartyNet is one window (K, November
    5th: "be sure that PPN never exists in more than one window").

    A member's room also hands over the chat it receives (keep_chat, keep_blob), which the app seals into this PC's own
    store, so the member keeps the history after the server closes, can read it while the host is off, and has it in a
    backup. The page is served by the host, so everything it hands over is checked and bounded, and a room is held to
    the one server it first named."""

    def __init__(self, origin, store=None, appearance=None, on_close=None, fullscreen=None, picture=None, look=None):
        self._origin = origin
        self._appearance = appearance  # a callable giving this person's accent, text size and spacing (Settings)
        self._on_close = on_close  # takes the room off the rail
        self._fullscreen_hook = fullscreen  # the whole window's fullscreen, shared by every room
        self._picture = picture  # a callable giving this person's own picture: {"hash", "data"} or None
        self._look = look  # a callable(name, picture bytes or None): the server's name and icon, for its place on the rail
        self._store = store  # a callable returning the store, or None for the host's own room (the server stores it)
        self._server = None

    def appearance(self):
        """How this person has every window look (their accent, text size and spacing), for the party page to apply."""
        return self._appearance() if self._appearance else None

    def set_fullscreen(self, on):
        """Make the window cover the whole display it is on, or put it back. The window host ignores the browser's own
        fullscreen request (an element would only fill the page inside the window's frame), so the page asks here."""
        if self._fullscreen_hook is None:
            return {"ok": False, "fullscreen": False}
        return self._fullscreen_hook(bool(on))

    def my_picture(self):
        """This person's own picture (Settings, My profile), for the room to hand to its server: {"hash", "data"} (base64)
        or {"hash": None} when there is none."""
        picture = self._picture() if self._picture else None
        return picture or {"hash": None, "data": ""}

    def server_look(self, name, icon_b64=""):
        """The room says what its server is called and its icon, for its place on the rail (kept for when the server is
        out of reach). Checked: a name of printable characters, a picture of a known kind up to the icon size."""
        from privatepartynet.server import MAX_PICTURE, sniff_image

        if not isinstance(name, str) or not isinstance(icon_b64, str) or self._look is None:
            return {"ok": False}
        name = "".join(c for c in name if c.isprintable()).strip()[:40]
        data = None
        if icon_b64:
            if len(icon_b64) > MAX_PICTURE * 4 // 3 + 8:
                return {"ok": False}
            try:
                data = base64.b64decode(icon_b64, validate=True)
            except ValueError:
                return {"ok": False}
            if sniff_image(data) is None:
                return {"ok": False}
        if not name:
            return {"ok": False}
        self._look(name, data)
        return {"ok": True}

    def keep_chat(self, server, server_name, channels, messages):
        """Save chat this window received into this PC's own encrypted store. Returns {ok, missing}: the pictures among
        these messages that this PC does not have yet (the page fetches them from the host and calls keep_blob)."""
        store = self._store() if self._store else None
        if store is None:
            return {"ok": False, "missing": []}
        if not (isinstance(server, str) and 8 <= len(server) <= 32 and HEX.match(server)):
            return {"ok": False, "missing": []}
        if self._server is None:
            self._server = server
        elif server != self._server:
            return {"ok": False, "missing": []}  # one window, one server: a page cannot write into another server's copy
        names = {}
        for channel in channels if isinstance(channels, list) else []:
            if isinstance(channel, dict) and isinstance(channel.get("id"), str) and len(channel["id"]) <= 32:
                names[channel["id"]] = {"name": str(channel.get("name") or "")[:32], "kind": "voice" if channel.get("kind") == "voice" else "text"}
        missing = []
        for message in (messages if isinstance(messages, list) else [])[:KEEP_BATCH]:
            kept = self._clean(message)
            if kept is None:
                continue
            channel, record = kept
            store.add_message(record["id"], f"srv:{server}:{channel}", record["user"], record["body"], ts=record["ts"],
                              seq=record["seq"])
            picture = record["body"].get("image")
            if picture and not store.has_blob(picture["hash"]) and picture["hash"] not in missing and len(missing) < KEEP_BLOBS:
                missing.append(picture["hash"])
        saved = store.server("seen-" + server) or {}
        merged = dict(saved.get("channels") or {})
        merged.update(names)
        store.save_server("seen-" + server, {"name": str(server_name or saved.get("name") or "Server")[:40],
                                             "server_id": server, "channels": merged, "origin": self._origin})
        return {"ok": True, "missing": missing}

    @staticmethod
    def _clean(message):
        if not isinstance(message, dict):
            return None
        message_id, channel = message.get("id"), message.get("channel")
        if not (isinstance(message_id, str) and 1 <= len(message_id) <= 32 and HEX.match(message_id)):
            return None
        if not (isinstance(channel, str) and 1 <= len(channel) <= 32):
            return None
        ts, seq = message.get("ts"), message.get("seq")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 0 < ts < 1e11:
            return None
        if not isinstance(seq, int) or isinstance(seq, bool):
            seq = None
        user = message.get("user") if isinstance(message.get("user"), str) else ""
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        body = {"text": "".join(c for c in text if c.isprintable() or c == " ")[:500],
                "name": "".join(c for c in str(message.get("name") or "") if c.isprintable())[:40],
                "from": str(message.get("from") or "")[:32], "channel": channel}
        picture = message.get("image")
        if isinstance(picture, dict) and isinstance(picture.get("hash"), str) and len(picture["hash"]) == 64 and HEX.match(picture["hash"]):
            body["image"] = {"hash": picture["hash"], "w": picture.get("w") if isinstance(picture.get("w"), int) else 0,
                             "h": picture.get("h") if isinstance(picture.get("h"), int) else 0}
        file = message.get("file")  # a file's description only: its bytes stay with the host (saved with Save)
        if isinstance(file, dict) and isinstance(file.get("id"), str) and len(file["id"]) == 32 and HEX.match(file["id"])                 and isinstance(file.get("size"), int) and not isinstance(file.get("size"), bool) and file["size"] >= 0:
            body["file"] = {"id": file["id"], "size": file["size"],
                            "name": "".join(c for c in str(file.get("name") or "file") if c.isprintable())[:200],
                            "type": "".join(c for c in str(file.get("type") or "") if c.isprintable())[:100]}
        if not body["text"] and "image" not in body and "file" not in body:
            return None
        return channel, {"id": message_id, "ts": float(ts), "seq": seq, "user": user[:64], "body": body}

    def keep_blob(self, digest, data_b64):
        """Save one picture the page fetched from the host. Kept only when it is the picture its name says it is."""
        import hashlib

        from privatepartynet.server import MAX_IMAGE, sniff_image

        store = self._store() if self._store else None
        if store is None or self._server is None or not isinstance(digest, str) or not isinstance(data_b64, str):
            return {"ok": False}
        if len(data_b64) > MAX_IMAGE * 4 // 3 + 8:
            return {"ok": False}
        try:
            data = base64.b64decode(data_b64, validate=True)
        except ValueError:
            return {"ok": False}
        if hashlib.sha256(data).hexdigest() != digest or sniff_image(data) is None:
            return {"ok": False}
        store.put_blob(data)
        return {"ok": True}

    def close_window(self):
        """Take the room off the rail. A room calls this when its member leaves the server."""
        if self._on_close is None:
            return {"ok": False, "message": "No room to close."}
        self._on_close()
        return {"ok": True, "message": ""}


# What a server's room inside the one window may ask of the app: WindowApi's own calls (test_app checks they match).
ROOM_CALLS = ("close_window", "set_fullscreen", "keep_chat", "keep_blob", "appearance", "my_picture", "server_look")


class Controller:
    """The app's state and actions. The control page calls these through the window bridge.

    Public methods here are what the page can call, so every one returns plain data.
    """

    def __init__(self, data_folder=None, discovery_port=DISCOVERY_PORT, grabber=None):
        self._data_dir = Path(data_folder) if data_folder else data_dir()
        self._settings = load_settings(self._data_dir)
        self._name = clean_name(self._settings.get("name") or socket.gethostname())
        self._grabber = grabber if grabber is not None else ScreenGrabber()
        self._feed = FeedServer(self._grabber)
        self._feed.start()
        self._cert_pem = ""
        self._party = None
        self._passcode = ""
        self._peers = []
        self._requests = {}  # id -> {id, name, address, at}
        self._notice = ""
        self._window = None
        self._vault = None
        self._store = None
        self._identity = None
        self._secure_error = ""
        self._open_secure()
        self._discovery = Discovery(self._name, port=discovery_port)
        self._discovery_error = ""
        try:
            self._discovery.start()
        except OSError as exc:
            log.exception("Could not start network discovery")
            self._discovery = None
            self._discovery_error = f"Nearby PCs are unavailable: {plain_error(exc)}"
        self._dm = None
        self._start_dm()
        self._probe_network = discovery_port != 0  # port 0 means a test or diagnostic run: no router or STUN calls
        self._reach = None  # how this PC's party can be reached from outside, once worked out
        self._upnp = None
        self._reach_run = 0
        self._reach_lock = threading.Lock()
        self._joiner = None
        self._joiner_opened = False
        self._game = None  # the game network in use: {key, session, link, mesh, paths, stop, public}
        self.path_manager = None  # the game network's paths (pathway.PathManager) while it is on
        self._game_launch = None  # tests replace how the elevated helper is started
        self._game_helper = {}  # tests: where the helper is and its in-memory adapter (GameSession's keywords)
        self._game_paths = None  # tests: make(identity) -> a path manager in place of pathway.PathManager
        self._game_note = ""
        self._host_punch = None  # while the party is open: reachable through the routers by invite (punch.py)
        # The game network's PathManager (pathway.py) while it runs, set by whoever starts it; with .names, a dict of
        # user id -> name, when it knows them. The control panel shows its relaying and its problems from it.
        self.path_manager = None
        self._party_windows = set()  # joined parties' windows that are open: an update waits for them to close
        # The one window (use_shell): its page server, the rooms in its frames (key -> WindowApi), and what its rail
        # should open or close next (shell_state hands each event over once).
        self._shell = None
        self._room_apis = {}
        self._shell_events = []
        self._shell_lock = threading.Lock()
        self._fullscreen = False
        self.updater = Updater(lambda: self._settings, self._save, self._data_dir, self._update_busy,
                               quit=self._quit_for_update)
        if self._probe_network:  # a test or diagnostic run (discovery port 0) never asks GitHub
            self.updater.start()
        self._tunnels = {}  # joined server key -> the punch-through tunnel it is reached by
        self._connecting = {}  # joined server key -> the Joiner reaching it again (connect_server)
        self._rail_note = {}  # joined server key -> {"state", "message"} when it could not be opened

    # --- the key, the identity and the chat store ----------------------------------------------------------

    def _open_secure(self, password=None):
        """Open the vault, then the identity and the encrypted chat store it protects. A vault with a password stays
        locked until unlock() is given it; anything else that goes wrong is kept as a message for the page."""
        try:
            if self._vault is None:
                self._vault = Vault(self._data_dir / "keys")
            self._vault.unlock(password)
            self._identity = Identity.load_or_create(self._vault, self._data_dir / "identity")
            if self._store is None:
                self._store = Store(self._vault, self._data_dir / "store")
            self._secure_error = ""
        except Locked:
            self._secure_error = ""
        except WrongPassword:
            self._secure_error = "That is not the password."
        except (VaultError, OSError) as exc:
            log.exception("Could not open the encrypted store")
            self._secure_error = f"Your saved chats could not be opened: {plain_error(exc)}"

    def unlock(self, password):
        """Give the password that protects the saved chats. Returns {ok, message}. Your server and your list of servers
        come back once it is open."""
        was_locked = self._store is None
        self._open_secure(str(password or ""))
        self._start_dm()
        if was_locked and self._store is not None and self._shell is not None:
            threading.Thread(target=self.start_saved, name="privatepartynet-start-saved", daemon=True).start()
        return {"ok": self._store is not None, "message": self._secure_error}

    def set_chat_password(self, new_password, old_password=None):
        """Protect the saved chats with a password, change it (the old one is needed), or pass an empty new password to
        remove it. Windows protects the key either way; a password adds a second lock. A forgotten password cannot be
        recovered."""
        if self._vault is None:
            return {"ok": False, "message": self._secure_error or "The saved chats are not open."}
        try:
            if new_password:
                self._vault.set_password(str(new_password), old_password)
            else:
                self._vault.remove_password(old_password)
        except WrongPassword:
            return {"ok": False, "message": "That is not the current password."}
        except (Locked, VaultError, ValueError) as exc:
            log.warning("The chat password was not changed: %s", exc)
            return {"ok": False, "message": plain_error(exc)}
        return {"ok": True, "message": ""}

    # --- direct messages -------------------------------------------------------------------------------------

    def _start_dm(self):
        """Start taking direct messages, once the identity and the store are open."""
        if self._dm is not None or self._store is None or self._identity is None:
            return
        try:
            certfile, keyfile = ensure_certificate(self._data_dir, ["localhost", "127.0.0.1", *self._addresses()])
            self._dm = DmService(self._identity, self._store, lambda: self._name, certfile, keyfile,
                                 resolver=self._resolve_dm)
            self._dm.start()
        except (OSError, ValueError):
            log.exception("Could not start direct messages")
            self._dm = None
            return
        if self._discovery is not None:
            self._discovery.set_identity(self._identity.user_id, self._identity.sign_public.hex(),
                                         self._identity.dh_public.hex(), self._dm.port)

    def _resolve_dm(self, user_id):
        """Where a person can be reached right now, from the beacons heard on the network."""
        if self._discovery is None:
            return None
        for peer in self._discovery.peer_list():
            if peer.get("user") == user_id and peer.get("dm"):
                return peer["address"], peer["dm"]
        return None

    def _live_users(self):
        if self._discovery is None:
            return {}
        return {peer["user"]: peer for peer in self._discovery.peer_list() if peer.get("user")}

    def dm_state(self):
        """Contacts (newest conversation first) and the people nearby who can be added, for the messages window."""
        if self._store is None:
            return {"ok": False, "message": self._secure_error or "The saved chats are locked.", "contacts": [], "nearby": []}
        live = self._live_users()
        contacts, known = [], set()
        for card in self._store.contacts():
            known.add(card["id"])
            scope = scope_for(card["id"])
            last = self._store.history(scope, limit=1)
            preview = ""
            if last:
                preview = last[0]["body"].get("text") or "Picture"
            contacts.append({"id": card["id"], "name": card.get("name", "Someone"), "online": card["id"] in live,
                             "unread": self._store.unread(scope, not_from=self._identity.user_id), "preview": preview[:80],
                             "last": last[0]["ts"] if last else card.get("added", 0), "new": bool(card.get("new")),
                             "waiting": len(self._dm.pending(card["id"])) if self._dm else 0})
        contacts.sort(key=lambda c: c["last"], reverse=True)
        nearby = [{"id": uid, "name": peer["name"], "address": peer["address"]} for uid, peer in live.items()
                  if uid not in known and uid != self._identity.user_id]
        return {"ok": True, "message": "", "contacts": contacts, "nearby": nearby, "me": self._identity.user_id,
                "dm_ready": self._dm is not None}

    def _reach_view(self):
        reach = self._reach
        if not reach or self._party is None:
            return None
        if reach.get("state") != "done":
            return {"state": reach.get("state")}
        return {"state": "done", "reachable": reach["reachable"], "advice": reach["advice"], "nat": reach["nat"],
                "cgnat": reach["cgnat"], "ipv6": bool(reach["ipv6"]), "port_open": bool(reach["upnp"].get("ok"))}

    def _dm_unread(self):
        if self._store is None:
            return 0
        return sum(self._store.unread(scope_for(card["id"]), not_from=self._identity.user_id) for card in self._store.contacts())

    def dm_add(self, user_id):
        """Add someone seen on the network as a contact, pinning the keys they announced."""
        peer = self._live_users().get(user_id)
        if peer is None or not peer.get("sign"):
            return {"ok": False, "message": "That person is no longer visible on the network."}
        self._store.save_contact({"id": user_id, "name": peer["name"], "sign": peer["sign"], "dh": peer["dh"],
                                  "addr": peer["address"], "port": peer.get("dm")})
        return {"ok": True, "message": ""}

    def dm_history(self, peer_id, before=None, limit=50):
        if self._store is None or self._store.contact(str(peer_id)) is None:
            return {"ok": False, "messages": []}
        scope = scope_for(str(peer_id))
        pending = self._dm.pending(str(peer_id)) if self._dm else set()
        messages = [{"id": m["id"], "mine": m["sender"] == self._identity.user_id, "text": m["body"].get("text", ""),
                     "name": m["body"].get("name", ""), "ts": m["ts"], "pending": m["id"] in pending,
                     "refused": bool(m["body"].get("refused")),
                     "image": (m["body"].get("image") or {}).get("hash")}
                    for m in self._store.history(scope, limit=max(1, min(int(limit), 200)), before_ts=before)]
        return {"ok": True, "messages": messages}

    def dm_send(self, peer_id, text, image_b64=None):
        if self._dm is None:
            return {"ok": False, "message": "Direct messages are not available right now."}
        try:
            picture = base64.b64decode(image_b64, validate=True) if image_b64 else None
            self._dm.send(str(peer_id), str(text or ""), picture)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "message": str(exc.args[0] if exc.args else exc)}
        return {"ok": True, "message": ""}

    def dm_image(self, digest):
        """A picture from a conversation as a data address for the page to show."""
        data = self._store.get_blob(str(digest)) if self._store is not None else None
        if data is None:
            return {"ok": False, "src": ""}
        from privatepartynet.server import sniff_image

        return {"ok": True, "src": f"data:{sniff_image(data) or 'application/octet-stream'};base64,"
                                   + base64.b64encode(data).decode("ascii")}

    def dm_read(self, peer_id):
        if self._store is not None:
            self._store.mark_read(scope_for(str(peer_id)))
            card = self._store.contact(str(peer_id))
            if card and card.get("new"):
                pass  # stays "new" until the person writes back or is added; opening it does not make them a friend
        return {"ok": True}

    def dm_safety(self, peer_id):
        card = self._store.contact(str(peer_id)) if self._store is not None else None
        if card is None:
            return {"ok": False, "number": ""}
        return {"ok": True, "number": safety_number(self._identity.card(), card)}

    def saved_servers(self):
        """Servers this PC keeps a copy of, as a member, for reading while the host is off."""
        if self._store is None:
            return []
        listed = []
        for item in self._store.server_items():
            if not item["server_id"].startswith("seen-"):
                continue
            record = item["record"]
            channels = [{"id": cid, "name": info.get("name") or cid} for cid, info in (record.get("channels") or {}).items()
                        if info.get("kind") != "voice"]
            listed.append({"server": record.get("server_id", ""), "name": record.get("name", "Server"), "channels": channels})
        return listed

    def saved_history(self, server, channel, before=None, limit=50):
        """A page of a saved server channel, newest last, read from this PC only."""
        if self._store is None or not isinstance(server, str) or not isinstance(channel, str):
            return {"ok": False, "messages": []}
        rows = self._store.history(f"srv:{server}:{channel}", limit=max(1, min(int(limit), 200)), before_ts=before)
        return {"ok": True, "messages": [{"id": m["id"], "name": m["body"].get("name", ""), "text": m["body"].get("text", ""),
                                          "ts": m["ts"], "image": (m["body"].get("image") or {}).get("hash"),
                                          "mine": m["sender"] == (self._identity.user_id if self._identity else "")}
                                         for m in rows]}

    def open_messages(self):
        return self._open_info("dm", "Direct messages", 980, 680)

    # --- reaching a party from outside, and joining one ------------------------------------------------------

    def _check_reach(self, port):
        """Work out, in the background, how people outside this network could reach the party: a router port-open,
        what the internet sees, IPv6. Takes a few seconds; state() shows the result when it is in."""
        self._reach_run += 1
        run = self._reach_run
        if not self._probe_network:
            return  # a test or diagnostic run (discovery port 0) must not open ports on a real router
        self._reach = {"state": "checking"}

        def work():
            mapping = connect.UpnpMapping(port)
            try:
                upnp = mapping.map()
                party = self._party
                if mapping.gateway_address and party is not None:
                    party.untrusted.add(mapping.gateway_address)
                report = connect.gather(port, upnp=upnp)
            except Exception:
                log.exception("Checking how the party can be reached failed")
                mapping.unmap()
                if run == self._reach_run:
                    self._reach = {"state": "failed"}
                return
            with self._reach_lock:
                if run == self._reach_run:
                    self._upnp = mapping
                    self._reach = {"state": "done", **report}
                    return
            mapping.unmap()  # the party was stopped (or restarted) while this was running

        threading.Thread(target=work, name="privatepartynet-reach", daemon=True).start()

    def _start_host_punch(self):
        """Be reachable through the routers for people holding an invite: a punched socket published on the DHT, and
        the invites' mailboxes watched. Started when the first invite is made, or at party start when invites exist.
        Skipped in tests and diagnostic runs, which must not touch the internet."""
        if not self._probe_network or self._host_punch is not None or self._party is None:
            return
        party = self._party
        certfile, keyfile = self._certfiles

        def work():
            from privatepartynet import punch

            try:
                host = punch.HostPunch(self._identity._sign, party.server_id, str(certfile), str(keyfile), party.port,
                                       party.invite_secrets, relays=party.host_relays,
                                       ipv6=connect.global_ipv6_addresses)
            except Exception:
                log.exception("Could not get ready to be reached through the routers")
                return
            if self._party is party:
                self._host_punch = host
            else:
                host.close()

        threading.Thread(target=work, name="privatepartynet-punch-start", daemon=True).start()

    def _end_host_punch(self):
        host, self._host_punch = self._host_punch, None
        if host is not None:
            threading.Thread(target=host.close, name="privatepartynet-punch-stop", daemon=True).start()

    def _end_reach(self):
        with self._reach_lock:
            self._reach_run += 1
            self._reach = None
            mapping, self._upnp = self._upnp, None
        if mapping is not None:
            mapping.unmap()

    def _invite_code(self):
        if self._party is None or not self._cert_pem:
            return ""
        if self._reach and self._reach.get("state") == "done":
            candidates = self._reach["candidates"]
        else:
            # The reach check (router, STUN) takes a few seconds after the party starts. An invite made before it is in
            # still carries every address this PC knows by itself, IPv6 included: 3.1 put only the LAN ones in it.
            port = self._party.port
            candidates = [["lan", a, port] for a in self._addresses()]
            candidates += [["ipv6", a, port] for a in connect.global_ipv6_addresses()]
            candidates = candidates[:connect.MAX_CANDIDATES]
        self._start_host_punch()
        secret = self._party.new_invite_secret()
        whole, spki = pins_of_pem(self._cert_pem)
        code = connect.encode_invite(self._party.party_name, self._party.passcode, candidates, whole,
                                     server_id=self._settings.get("server_id") or "",
                                     host_sign=self._identity.sign_public.hex() if self._identity else "",
                                     secret=secret, relays=self._party.invite_relays(bytes.fromhex(secret)), spki=spki)
        log.info("Invite made: %s (addresses: %s)", connect.describe_invite(connect.decode_invite(code)),
                 ", ".join(f"{address} ({kind}{', ' + connect.vpn_name(address) if connect.vpn_name(address) else ''})"
                           for kind, address, _ in candidates) or "none")
        return code

    def invite(self):
        """The invite code for this party: the name, the password, where to try and the certificate to check."""
        code = self._invite_code()
        if not code:
            return {"ok": False, "invite": "", "message": "Start your server first."}
        return {"ok": True, "invite": code, "message": ""}

    def join_invite(self, code):
        """Ask to join a party from an invite code. Watch join_status() for the answer. Returns {ok, message}."""
        try:
            invite = connect.decode_invite(code)
        except connect.ConnectError as exc:
            return {"ok": False, "message": str(exc)}
        return self._begin_join(invite)

    def join_address(self, address, port, password, name=""):
        """Ask to join a party by address and password, without an invite (its certificate is then taken on trust the
        first time, and remembered)."""
        address = str(address or "").strip().strip("[]")
        try:
            import ipaddress

            ipaddress.ip_address(address)
            port = int(port or DEFAULT_PORT)
            if not 0 < port < 65536:
                raise ValueError("port")
        except ValueError:
            return {"ok": False, "message": "Enter the host's address as numbers (like 203.0.113.5) and a port from 1 to 65535."}
        kind = "ipv6" if ":" in address else "lan" if address.startswith(("10.", "192.168.", "172.")) else "public"
        invite = {"name": str(name or "Server")[:40], "password": str(password or ""), "candidates": [[kind, address, port]],
                  "fingerprint": "", "server_id": "", "host_sign": "", "source": "address"}
        return self._begin_join(invite)

    def _begin_join(self, invite, peer=None):
        """`peer`: the Nearby PC this join asks, so its answer (a password wanted, say) shows there."""
        if self._joiner is not None and self._joiner.status["state"] in ("trying", "waiting"):
            return {"ok": False, "message": "Already asking to join a party. Cancel that first."}
        if peer is None:
            self._add_nearby(invite)
        if not invite["candidates"] and not invite.get("secret"):
            return {"ok": False, "message": "That invite has no address to try."}
        if self._identity is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        self._joiner = connect.Joiner(invite, self._name, identity=self._identity)
        self._joiner.peer_id = peer
        self._joiner_opened = False
        threading.Thread(target=self._joiner.run, name="privatepartynet-join", daemon=True).start()
        return {"ok": True, "message": "Asking..."}

    def _add_nearby(self, invite):
        """When the invite's host is also in the Nearby list (seen over a VPN such as Radmin, or a network the invite
        does not know of), its address there is tried first: the same certificate proves it is the same party."""
        pins = {invite.get("fingerprint") or "", invite.get("spki") or ""} - {""}
        if self._discovery is None or not pins:
            return
        known = {(address, port) for _, address, port in invite["candidates"]}
        for peer in self._discovery.peer_list():
            if peer.get("sharing") and pins & {peer.get("fp"), peer.get("spki")} and peer.get("port"):
                if (peer["address"], peer["port"]) not in known:
                    log.info("The invite's host is also nearby, at %s%s: trying that address first", peer["address"],
                             f" ({connect.vpn_name(peer['address'])})" if connect.vpn_name(peer["address"]) else "")
                    invite["candidates"].insert(0, ["lan", peer["address"], int(peer["port"])])

    def join_status(self):
        """Where the join stands: {state, message, needs_password, peer}. When the owner has let you in, the party
        window opens here. `peer` is the Nearby PC asked (None for an invite or an address); `needs_password` says
        the party wants a password that was not given."""
        joiner = self._joiner
        if joiner is None:
            return {"state": "idle", "message": ""}
        status = dict(joiner.status, needs_password=joiner.needs_password, peer=getattr(joiner, "peer_id", None))
        if status["state"] == "allowed":
            if not self._joiner_opened:
                self._joiner_opened = True
                invite = joiner.invite
                key = self._remember_server(invite, joiner)
                # The room accepts the certificate this join checked, and no other.
                self._join({"url": status["url"], "name": invite["name"], "key": key,
                            "pins": (joiner.seen_fingerprint, joiner.seen_spki), "embed": getattr(joiner, "embed", False)})
            status["state"], status["message"] = "joined", "You are in. The server is on your list at the left."
        status.pop("url", None)
        return status

    def join_cancel(self):
        if self._joiner is not None:
            self._joiner.cancel()
        return {"ok": True}

    def _remember_server(self, invite, joiner):
        """Put the server on this PC's list for good (K, November 5th: joining a server should add it to the list and it
        should stay there), with what is needed to reach it again: where to try, the password and the host's
        certificate. Returns its key on the list ("joined-<server id>"), or None when there is no store."""
        if self._store is None:
            return None
        fingerprint = invite["fingerprint"] or joiner.seen_fingerprint
        spki = invite.get("spki") or joiner.seen_spki
        key = invite["server_id"] or (fingerprint[:16] if fingerprint else "")
        if not key:
            return None
        key = "joined-" + key
        old = self._store.server(key) or {}
        self._store.save_server(key, {**old, "name": old.get("name") or invite["name"], "candidates": invite["candidates"],
                                      "password": invite["password"], "fingerprint": fingerprint, "spki": spki,
                                      "server_id": invite["server_id"], "joined": True,
                                      "host_sign": invite.get("host_sign", ""), "secret": invite.get("secret", ""),
                                      "joined_at": old.get("joined_at") or time.time()})
        if joiner.tunnel is not None:
            previous = self._tunnels.pop(key, None)
            if previous is not None and previous is not joiner.tunnel:
                previous.close()
            self._tunnels[key] = joiner.tunnel
        return key

    def joined_servers(self):
        """The servers on this PC's list, in the order they were joined: the rail shows them, the game network page
        offers them. {key, name, icon} (icon: the hash of the picture kept for it, or "")."""
        if self._store is None:
            return []
        items = [(item["record"].get("joined_at") or 0, item["server_id"], item["record"])
                 for item in self._store.server_items() if item["server_id"].startswith("joined-")]
        items.sort(key=lambda item: (item[0], item[1]))
        return [{"key": key, "name": record.get("name") or "Server", "icon": record.get("icon") or ""}
                for _, key, record in items]

    @staticmethod
    def _invite_of(record):
        return {"name": record.get("name") or "Server", "password": record.get("password", ""),
                "candidates": record.get("candidates") or [], "fingerprint": record.get("fingerprint", ""),
                "spki": record.get("spki", ""), "server_id": record.get("server_id", ""),
                "host_sign": record.get("host_sign", ""), "secret": record.get("secret", ""), "source": "remembered"}

    def connect_server(self, key):
        """Reach a server on the list again: at start, and with Try again on the rail. Someone the server let in before
        comes straight back in, with no one asked; the room then opens in its place on the rail (shell_state)."""
        key = str(key or "")
        record = self._store.server(key) if self._store is not None and key.startswith("joined-") else None
        if record is None:
            return {"ok": False, "message": "That server is no longer on your list."}
        if key in self._room_apis:
            return {"ok": True, "message": ""}
        running = self._connecting.get(key)
        if running is not None and running.status["state"] in ("trying", "waiting"):
            return {"ok": True, "message": ""}
        if self._identity is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        invite = self._invite_of(record)
        if not invite["candidates"] and not invite["secret"]:
            self._rail_note[key] = {"state": "offline", "message": "No address is known for this server."}
            return {"ok": False, "message": "No address is known for this server."}
        joiner = connect.Joiner(invite, self._name, identity=self._identity)
        joiner.peer_id = None
        self._connecting[key] = joiner
        self._rail_note.pop(key, None)
        threading.Thread(target=joiner.run, name="privatepartynet-reconnect", daemon=True).start()
        return {"ok": True, "message": ""}

    def _check_connecting(self):
        """Open the servers that answered since the last look, and note the ones that could not be reached."""
        for key, joiner in list(self._connecting.items()):
            status = joiner.status
            if status["state"] in ("trying", "waiting"):
                continue
            del self._connecting[key]
            if status["state"] == "allowed":
                self._remember_server(joiner.invite, joiner)
                record = self._store.server(key) if self._store is not None else None
                # select False: a server coming back on its own does not take over what the person is looking at; one
                # they are looking at (its card, Try again) opens right there
                self._join({"url": status["url"], "name": (record or {}).get("name") or joiner.invite["name"],
                            "key": key, "pins": (joiner.seen_fingerprint, joiner.seen_spki),
                            "embed": getattr(joiner, "embed", False), "select": False})
            else:
                self._rail_note[key] = {"state": "offline", "message": status.get("message") or "The server did not answer."}

    def rejoin(self, key):
        """Home's list of servers: reach one again and show it."""
        result = self.connect_server(key)
        if result["ok"]:
            self._shell_queue({"type": "show", "key": str(key)})
        return result

    def forget_joined(self, key):
        """Leave a server for good: it goes from the list and the rail. Its saved messages stay in the store."""
        key = str(key or "")
        if not key.startswith("joined-"):
            return {"ok": False, "message": "That is not a server you joined."}
        joiner = self._connecting.pop(key, None)
        if joiner is not None:
            joiner.cancel()
        self._rail_note.pop(key, None)
        tunnel = self._tunnels.pop(key, None)
        if tunnel is not None:
            tunnel.close()
        if key in self._room_apis:
            self._shell_room_closed(key)
        if self._store is not None:
            self._store.forget_server(key)
        if self._game is not None and self._game["key"] == key:
            self.game_off()
        return {"ok": True, "message": ""}

    def _server_look(self, key, name, data):
        """A joined server's room said its name and icon: keep them for its place on the rail."""
        if self._store is None or not key.startswith("joined-"):
            return
        record = self._store.server(key)
        if record is None:
            return
        icon = self._store.put_blob(data) if data is not None else ""
        if record.get("name") != name or (record.get("icon") or "") != icon:
            self._store.save_server(key, {**record, "name": name, "icon": icon})

    # --- the game network ----------------------------------------------------------------------------------

    def _game_networks(self):
        listed = []
        if self._party is not None:
            listed.append({"key": "own", "name": f"{self._name} (your server)"})
        for server in self.joined_servers():
            listed.append({"key": server["key"], "name": server["name"]})
        return listed

    def game_state(self):
        """The game networks this PC can use, which one is in use (the green light), its address and how it is doing."""
        game = self._game
        view = {"networks": self._game_networks(), "active": game["key"] if game else "",
                "chosen": self._settings.get("game_server") or "", "state": "off", "message": self._game_note,
                "address": "", "network": "", "members": []}
        if game is not None:
            session, link, mesh = game.get("session"), game.get("link"), game.get("mesh")
            if session is not None:
                view.update(state=session.status["state"], message=session.status["message"], address=session.address,
                            network=session.network)
                if link is not None and link.status["state"] == "retrying":
                    view["message"] = (view["message"] + " The host is out of reach; members already on keep playing."
                                       ).strip()
            elif link is not None:
                view.update(state="connecting", message=link.status["message"] or "Connecting to the host...")
            if mesh is not None:
                view["members"] = [{"address": m["address"], "way": m["kind"]} for m in mesh.members()]
        return view

    def _make_paths(self):
        """The paths to the other members: straight, through a hole punched in both routers, through a friend's PC,
        or through the relay (pathway.py)."""
        if self._game_paths is not None:
            return self._game_paths(self._identity)
        from privatepartynet import pathway

        return pathway.PathManager(self._identity, self.path_settings())

    @staticmethod
    def _use_relay(paths, relay, host_sign):
        """Hand the path manager the relay settings that came with the list, signed by the host."""
        if not relay or not hasattr(paths, "set_relay_config"):
            return
        try:
            from privatepartynet import pathway

            paths.set_relay_config(pathway.open_relay_config(relay, host_sign))
        except Exception:
            log.warning("The relay settings from the host could not be used", exc_info=True)

    def _game_session(self, game, address, network):
        session = GameSession(address, network, game["private"], self._data_dir / "keys", launch=self._game_launch,
                              open_services=self._open_services(), **self._game_helper)
        game["session"] = session
        game["mesh"] = GameMesh(self._identity.user_id, game["paths"], session)
        session.start()

    def game_use(self, key):
        """Use one server's game network: this PC gets that server's address on an adapter Windows and games see, and
        reaches each member over WireGuard. Only one at a time; switching turns the other off. Windows asks for
        permission to add the adapter."""
        key = str(key or "")
        self.game_off()
        if self._identity is None or self._vault is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        stop = threading.Event()
        if key == "own":
            if self._party is None:
                return {"ok": False, "message": "Start your server first: its game network runs while it is open."}
            server_key = self._party.server_id
        else:
            record = self._store.server(key) if self._store is not None and key.startswith("joined-") else None
            if not record or not (record.get("fingerprint") or record.get("spki")):
                return {"ok": False, "message": "That server is not in your list."}
            tunnel = self._tunnels.get(key)
            ordered = [["punch", "127.0.0.1", tunnel.port]] if tunnel is not None else connect._order(record.get("candidates") or [])
            if not ordered:
                return {"ok": False, "message": "No address is known for that server."}
            server_key = key
        try:
            private = gamenet.load_key(self._vault, self._data_dir / "identity", server_key)
            paths = self._make_paths()
        except Exception as exc:
            log.exception("The game network could not start")
            return {"ok": False, "message": f"The game network could not start: {plain_error(exc)}"}
        public = gamenet.public_key(private)
        game = {"key": key, "session": None, "link": None, "mesh": None, "paths": paths, "stop": stop,
                "private": private, "public": public}
        self._game = game
        self.path_manager = paths
        if key == "own":
            party = self._party
            try:
                address = party.net_host(public, paths.local_candidates())
            except gamenet.NetError as exc:
                log.warning("The game network could not start: %s", exc)
                self.game_off()
                return {"ok": False, "message": f"The game network could not start: {plain_error(exc)}"}
            paths.set_on_candidates(lambda candidates: party.net_candidates(party.net_host_user, candidates))
            self._game_session(game, address, party.net_directory().network)

            def follow():
                version, sent = None, 0.0
                while not stop.is_set():
                    current = party.net_wait(version, 0.5)
                    stale = time.monotonic() - sent >= gamenet.RELAY_REFRESH_SECONDS  # fresh relay settings
                    if (current == version and not stale) or stop.is_set():
                        continue
                    version, sent = current, time.monotonic()
                    signed = party.net_roster(party.net_host_user, self._identity.sign_public.hex())
                    self._use_relay(paths, signed.get("relay"), self._identity.sign_public.hex())
                    game["mesh"].apply(gamenet.open_roster(signed))

            threading.Thread(target=follow, name="privatepartynet-game-host", daemon=True).start()
        else:
            _, address, port = ordered[0]
            host_sign = record.get("host_sign", "")

            def on_roster(roster, relay):
                if stop.is_set():
                    return
                if game["session"] is None:
                    self._game_session(game, link.status["address"], link.status["network"])
                self._use_relay(paths, relay, host_sign)
                game["mesh"].apply(roster)

            link = NetLink(address, port, (record.get("fingerprint", ""), record.get("spki", "")), self._identity,
                           public, on_roster, candidates=paths.local_candidates(), host_sign=host_sign,
                           server_id=record.get("server_id", ""))
            game["link"] = link
            paths.set_on_candidates(link.set_candidates)
            link.start()

            def watch():
                while not stop.wait(0.2):
                    if link.status["state"] == "refused":  # not a member any more, or a guest now
                        self._game_note = link.status["message"]
                        if self._game is game:
                            self.game_off()
                        return

            threading.Thread(target=watch, name="privatepartynet-game-wait", daemon=True).start()
        self._game_note = ""
        self._save(game_server=key)
        return {"ok": True, "message": "Turning the game network on. Windows will ask for permission."}

    def _open_services(self):
        return [g for g in self._settings.get("game_open") or [] if g in gamenet.SERVICE_IDS]

    def game_services(self):
        """What party members can reach on this PC besides games: each group of Windows services, all closed unless the
        owner of this PC opened them. A setting of this PC only; it is never sent to the party."""
        opened = set(self._open_services())
        return {"ok": True, "groups": [{**group, "open": group["id"] in opened} for group in gamenet.SERVICE_GROUPS]}

    def game_set_service(self, group, open_it):
        """Open or close one group. A game network that is on takes it at once, with no new permission prompt."""
        if group not in gamenet.SERVICE_IDS:
            return {"ok": False, "message": "There is no such group of services."}
        opened = set(self._open_services())
        if open_it:
            opened.add(group)
        else:
            opened.discard(group)
        self._save(game_open=sorted(opened))
        game = self._game
        if game is not None and game.get("session") is not None:
            game["session"].set_open_services(sorted(opened))
        return self.game_services()

    def game_off(self):
        game, self._game = self._game, None
        if game is None:
            return {"ok": True, "message": ""}
        self.path_manager = None
        game["stop"].set()
        if game.get("link") is not None:
            game["link"].stop()
        if game["key"] == "own" and self._party is not None:
            self._party.net_host_off(game["public"])
        if game.get("session") is not None:
            game["session"].stop()
        if game.get("mesh") is not None:
            game["mesh"].stop()
        game["paths"].stop()
        self.updater.busy_changed()
        return {"ok": True, "message": "The game network is off."}

    # --- backup and import ---------------------------------------------------------------------------------

    def _need_store(self):
        if self._store is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        return None

    def _export_to(self, path, passphrase, include_identity):
        report = backup.export_all(self._store, passphrase, path,
                                   identity=self._identity if include_identity else None)
        extra = " Your identity is in the file, so anyone with the file and the passphrase can be you." if report["identity"] else ""
        return {"ok": True, "message": f"Saved {report['messages']:,} messages and {report['blobs']:,} pictures "
                                       f"from {report['scopes']} conversations to {report['path']}.{extra}"}

    def _import_from(self, path, passphrase, use_identity):
        report = backup.import_backup(self._store, path, passphrase)
        message = (f"Added {report['messages_added']:,} messages and {report['blobs_added']:,} pictures; "
                   f"{report['messages_skipped']:,} messages were already here.")
        if use_identity:
            keys = backup.read_identity(path, passphrase)
            if keys is None:
                message += " The file has no identity in it, so yours is unchanged."
            else:
                self._replace_identity(keys)
                message += " Your identity is now the one in the file."
        elif report["has_identity"]:
            message += " The file also carries an identity; tick the box to use it."
        return {"ok": True, "message": message}

    def _replace_identity(self, keys):
        """Make the identity from a backup this PC's identity. Only when no party is running on this PC."""
        identity = Identity.from_private_bundle(keys)
        if self._identity is not None and identity.user_id == self._identity.user_id:
            return
        folder = self._data_dir / "identity"
        folder.mkdir(parents=True, exist_ok=True)
        current = folder / "identity.bin"
        if current.exists():
            # The identity being replaced is kept (still sealed), so a mistaken import can be undone by hand.
            os.replace(current, folder / time.strftime("identity-before-import-%Y%m%d-%H%M%S.bin"))
        temp = folder / "identity.tmp"
        temp.write_bytes(self._vault.seal("identity", identity.private_bundle(), b"identity"))
        os.replace(temp, current)
        self._identity = identity
        if self._dm is not None:  # the message listener and the beacon still carried the old keys
            self._dm.stop()
            self._dm = None
        self._start_dm()

    def _ask_path(self, save, name=""):
        """A native file dialog on the control window. Returns a path, or None when the person cancels."""
        if self._window is None:
            return None
        dialog = getattr(webview, "FileDialog", None)
        kind = (dialog.SAVE if save else dialog.OPEN) if dialog else (webview.SAVE_DIALOG if save else webview.OPEN_DIALOG)
        types = (f"PrivatePartyNet backup (*{backup.EXTENSION})",)
        if save:
            chosen = self._window.create_file_dialog(kind, save_filename=name, file_types=types)
        else:
            chosen = self._window.create_file_dialog(kind, allow_multiple=False, file_types=types)
        if not chosen:
            return None
        return chosen if isinstance(chosen, str) else chosen[0]

    def export_chats(self, passphrase, include_identity=False):
        """Save every chat, its pictures, contacts and servers to one file, sealed with the passphrase. Asks where."""
        refused = self._need_store()
        if refused:
            return refused
        if not passphrase:
            return {"ok": False, "message": "Choose a passphrase for the backup file first."}
        path = self._ask_path(True, time.strftime("PrivatePartyNet chats %Y-%m-%d") + backup.EXTENSION)
        if path is None:
            return {"ok": False, "message": "Cancelled."}
        try:
            return self._export_to(path, str(passphrase), bool(include_identity))
        except (backup.BackupError, OSError) as exc:
            log.exception("Export failed")
            return {"ok": False, "message": f"The backup could not be saved: {plain_error(exc)}"}

    def import_chats(self, passphrase, use_identity=False):
        """Merge a backup file into the saved chats. Asks which file. Nothing already here is changed or removed."""
        refused = self._need_store()
        if refused:
            return refused
        path = self._ask_path(False)
        if path is None:
            return {"ok": False, "message": "Cancelled."}
        if use_identity and self._party is not None:
            return {"ok": False, "message": "Stop your server first: the identity cannot change while it is running."}
        try:
            return self._import_from(path, str(passphrase or ""), bool(use_identity))
        except (backup.BackupError, VaultError, OSError) as exc:
            log.warning("Import failed", exc_info=isinstance(exc, OSError))
            return {"ok": False, "message": plain_error(exc)}

    # --- wiring from the app entry point -----------------------------------------------------------------

    def bind_window(self, window):
        self._window = window
        if self._shell is not None:
            window.ppn_refused = self._shell_refused  # windowpins: a refused room shows its refusal in its own place

    # --- the one window ----------------------------------------------------------------------------------------

    def use_shell(self, shell):
        """Show everything in one window (shell.html, served by `shell`): rooms open in its frames, About and How over it."""
        self._shell = shell

    def _shell_queue(self, event):
        with self._shell_lock:
            self._shell_events.append(event)
            del self._shell_events[:-200]

    def shell_state(self):
        """What the one window's rail needs, asked for every second: unread direct messages, the theme and look, every
        server on this PC's list with how it stands, and the rooms to open or close and the pages to show since it last
        asked (each once)."""
        self._check_connecting()
        with self._shell_lock:
            events, self._shell_events = self._shell_events, []
        return {"dm_unread": self._dm_unread(), "theme": self._settings.get("theme", "dark"),
                "appearance": self.appearance(), "sounds": self.sounds(), "running": self._party is not None,
                "servers": self._rail_servers(), "picture": self._settings.get("picture") or "",
                "name": self._name, "events": events}

    def _rail_servers(self):
        """The rail's list (K, November 5th: a server you start or join goes on your list and stays there): your own
        server once you have made one, then every server joined, oldest first. state: "open" (its room is in its place),
        "starting", "stopped" (your own, not running), "connecting", "offline" (could not be reached; `message` says
        why)."""
        listed = []
        if self._settings.get("server_id"):
            state = "open" if "own" in self._room_apis else "starting" if self._party is not None else "stopped"
            listed.append({"key": "own", "name": self.server_name(), "own": True, "state": state, "message": "",
                           "icon": self._own_icon()})
        for server in self.joined_servers():
            key = server["key"]
            if key in self._room_apis:
                state, message = "open", ""
            elif key in self._connecting:
                state, message = "connecting", self._connecting[key].status.get("message") or ""
            else:
                note = self._rail_note.get(key) or {}
                state, message = note.get("state", "offline"), note.get("message", "")
            listed.append({"key": key, "name": server["name"], "own": False, "state": state, "message": message,
                           "icon": server["icon"]})
        return listed

    def server_icon(self, key):
        """A server's icon for its place on the rail: {"ok", "src"} (a data: address, "" when it has none)."""
        key = str(key or "")
        if key == "own":
            digest = self._own_icon()
        else:
            record = self._store.server(key) if self._store is not None and key.startswith("joined-") else None
            digest = (record or {}).get("icon") or ""
        return {"ok": True, "src": self._picture_src(digest)}

    def _picture_src(self, digest):
        from privatepartynet.server import sniff_image

        if not digest or self._store is None:
            return ""
        try:
            data = self._store.get_blob(digest)
        except VaultError:
            return ""
        if not data or sniff_image(data) is None:
            return ""
        return f"data:{sniff_image(data)};base64," + base64.b64encode(data).decode("ascii")

    def _own_icon(self):
        if self._party is not None:
            return self._party.icon() or ""
        record = self._store.server(self._settings["server_id"]) if self._store is not None and self._settings.get("server_id") else None
        return (record or {}).get("icon") or ""

    def server_name(self):
        """Your own server's name: the running server's (the owner or an admin may have renamed it from its room), or the
        saved one."""
        if self._party is not None:
            return self._party.party_name
        record = self._store.server(self._settings["server_id"]) if self._store is not None and self._settings.get("server_id") else None
        return (record or {}).get("name") or self._settings.get("server_name") or f"{self._name}'s server"

    def rename_server(self, name):
        """Name your own server (Home, Your server). Its room's server settings do the same for the owner and admins."""
        name = "".join(c for c in str(name or "") if c.isprintable()).strip()[:40]
        if not name:
            return {"ok": False, "message": "Give the server a name."}
        self._save(server_name=name)
        if self._party is not None:
            self._party.rename_server(self._party.host_id, name)
        elif self._store is not None and self._settings.get("server_id"):
            record = self._store.server(self._settings["server_id"])
            if record is not None:
                self._store.save_server(self._settings["server_id"], {**record, "name": name})
        return {"ok": True, "message": ""}

    def set_server_autostart(self, on):
        self._save(server_autostart=bool(on))
        return {"ok": True, "message": ""}

    # --- your own picture ------------------------------------------------------------------------------------------

    def set_picture(self, data_b64):
        """Your picture (Settings, My profile), shown beside your name on every server you are in and on the rail. Kept
        at its own size; up to 2 MB. An empty string takes it away. Rooms hand it to their servers (WindowApi.my_picture)."""
        from privatepartynet.server import MAX_PICTURE, MAX_PICTURE_TEXT, sniff_image

        if self._store is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        if not data_b64:
            self._save(picture="")
            return {"ok": True, "message": "", "src": ""}
        if not isinstance(data_b64, str) or len(data_b64) > MAX_PICTURE * 4 // 3 + 8:
            return {"ok": False, "message": f"A picture can be up to {MAX_PICTURE_TEXT}."}
        try:
            data = base64.b64decode(data_b64, validate=True)
        except ValueError:
            return {"ok": False, "message": "That picture could not be read."}
        if len(data) > MAX_PICTURE:
            return {"ok": False, "message": f"A picture can be up to {MAX_PICTURE_TEXT}."}
        if sniff_image(data) is None:
            return {"ok": False, "message": "Use a PNG, JPEG, GIF or WebP picture."}
        self._save(picture=self._store.put_blob(data))
        return {"ok": True, "message": "", "src": self._picture_src(self._settings["picture"])}

    def picture(self):
        """Your picture for the page: {"hash", "src"}."""
        digest = self._settings.get("picture") or ""
        return {"hash": digest, "src": self._picture_src(digest)}

    def _my_picture(self):
        digest = self._settings.get("picture") or ""
        if not digest or self._store is None:
            return None
        try:
            data = self._store.get_blob(digest)
        except VaultError:
            return None
        return {"hash": digest, "data": base64.b64encode(data).decode("ascii")} if data else None

    def room_call(self, key, method, args):
        """A server's room inside the one window asks the app for something (roombridge.js, through appshell.js, which
        checked it came from that room's frame). Only WindowApi's own calls, and only for the room that asked."""
        api = self._room_apis.get(key) if isinstance(key, str) else None
        if api is None or method not in ROOM_CALLS or not isinstance(args, (list, tuple)) or len(args) > 6:
            return {"ok": False, "message": "That cannot be done here."}
        try:
            return getattr(api, method)(*args)
        except TypeError:
            return {"ok": False, "message": "That request was not understood."}

    def _shell_room(self, key, name, url, origin, own=False, select=True, store=None):
        """Put a server's room in its place on the rail (or show it when it is there already)."""
        if key not in self._room_apis:
            api = WindowApi(origin, store=store, appearance=self.appearance,
                            on_close=lambda: self._shell_room_closed(key), fullscreen=self._set_fullscreen,
                            picture=self._my_picture, look=lambda name, data: self._server_look(key, name, data))
            self._room_apis[key] = api
            if not own:
                self._party_windows.add(key)
                self.updater.busy_changed()
        self._shell_queue({"type": "open", "key": key, "name": name, "url": url, "own": own, "select": select})

    def _shell_room_closed(self, key):
        self._room_apis.pop(key, None)
        if key in self._party_windows:
            self._party_windows.discard(key)
            self.updater.busy_changed()
        if self._fullscreen:
            self._set_fullscreen(False)
        self._shell_queue({"type": "close", "key": key})

    def _open_own_room(self, select):
        if self._party is None:
            return
        port = self._party.local_port
        self._shell_room("own", self._party.party_name, f"http://127.0.0.1:{port}/host", f"http://127.0.0.1:{port}",
                         own=True, select=select)

    def start_saved(self):
        """When the app opens: start your own server if it is set to (on unless turned off in Home), and reach every
        server on your list again, so the rail is as it was. Runs once the window is up (webview.start), and again
        after the saved chats' password is given."""
        if self._store is None or self._identity is None:
            return
        if self._settings.get("server_id") and self._settings.get("server_autostart", True) and self._party is None:
            result = self._start(self._name, self._settings["port"], select=False)
            if not result["ok"]:
                log.warning("Your server did not start with the app: %s", result["message"])
        for server in self.joined_servers():
            self.connect_server(server["key"])

    def _shell_refused(self, refused_origin):
        self._shell_queue({"type": "refused", "origin": refused_origin})

    def _set_fullscreen(self, on):
        window = self._window
        if window is None:
            return {"ok": False, "fullscreen": False}
        if on != self._fullscreen:
            window.toggle_fullscreen()
            self._fullscreen = on
        return {"ok": True, "fullscreen": on}

    # --- state for the page ------------------------------------------------------------------------------

    def state(self):
        self._drain()
        party = self._party
        return {
            "version": __version__,
            "app_name": APP_NAME,
            "theme": self._settings.get("theme", "dark"),
            "appearance": self.appearance(),
            "sounds": self.sounds(),
            "name": self._name,
            "port": self._settings["port"],
            "running": party is not None,
            "addresses": [f"https://{ip}:{party.port}" for ip in self._addresses()] if party else [],
            "passcode": party.passcode if party else "",
            "others": max(0, party.member_count - 1) if party else 0,
            "peers": list(self._peers),
            "requests": self._all_requests(),
            "join": self.join_status() if self._joiner is not None else None,
            "notice": self._notice,
            "problem": (party.last_error if party else "") or self._discovery_error or self._secure_error,
            "locked": self._store is None and self._vault is not None and self._vault.has_password,
            "chat_password": bool(self._vault and self._vault.has_password),
            "user_id": self._identity.user_id if self._identity else "",
            "server": {"made": bool(self._settings.get("server_id")), "name": self.server_name() if self._settings.get("server_id") else "",
                       "autostart": self._settings.get("server_autostart", True) is not False},
            "picture": self._settings.get("picture") or "",
            "dm_unread": self._dm_unread(),
            "reach": self._reach_view(),
            "game": self.game_state(),
            "relay": party.relay_view() if party else None,
            "update": self.updater.view(),
            "friend_relay": {"on": self._settings.get("friend_relay") is True,
                             "mbps": self._settings.get("friend_relay_mbps", 8), **self._paths_view()},
        }

    def _all_requests(self):
        """Everyone waiting to be let in: asks over the network beacon, and asks at the party's front door."""
        listed = sorted(self._requests.values(), key=lambda r: r["at"])
        listed = [{"id": r["id"], "name": r["name"], "address": r["address"]} for r in listed]
        if self._party is not None:
            listed += [{"id": "door:" + r["id"], "name": r["name"], "address": r["address"]}
                       for r in self._party.pending_requests()]
        return listed

    def _addresses(self):
        addresses = lan_addresses()
        return [a for a in addresses if a != LOOPBACK] or []

    def _drain(self):
        """Apply what the network said since the last poll. Expired join requests are dropped here too."""
        if self._discovery is not None:
            while True:
                try:
                    kind, data = self._discovery.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "peers":
                    self._peers = [self._peer_view(p) for p in data]
                elif kind == "request":
                    self._requests[data["id"]] = {**data, "at": time.monotonic()}
                elif kind == "accepted":
                    self._notice = f"{data['name']} let you in. The server is on your list at the left."
                    data["key"] = self._remember_accepted(data)
                    self._join(data)
                elif kind == "declined":
                    self._notice = f"{data['name']} said no."
                elif kind == "timeout":
                    self._notice = "No answer. Ask the host to start the party, then try again."
                elif kind == "error":
                    log.warning("Discovery: %s", data)
        now = time.monotonic()
        for request_id in [k for k, r in self._requests.items() if now - r["at"] > REQUEST_LIFETIME]:
            del self._requests[request_id]

    def _peer_view(self, peer):
        if peer["sharing"]:
            state = "server open"
        else:
            state = "idle"
        return {"id": peer["id"], "name": peer["name"], "address": peer["address"], "state": state}

    # --- actions ------------------------------------------------------------------------------------------

    def start(self, name, port):
        """Start your server on this PC (making it the first time). Returns {ok, message}."""
        return self._start(name, port, select=True)

    def start_own(self):
        """Start your own server from its place on the rail (it keeps its name, port and everything else)."""
        if not self._settings.get("server_id"):
            return {"ok": False, "message": "Create your server on Home first."}
        return self._start(self._name, self._settings["port"], select=True)

    def create_server(self, server_name, name, port):
        """Make your own server (the + on the rail, then Home): it goes on your list for good and starts."""
        server_name = "".join(c for c in str(server_name or "") if c.isprintable()).strip()[:40]
        if not server_name:
            return {"ok": False, "message": "Give your server a name."}
        self._save(server_name=server_name)
        result = self._start(name, port, select=True)
        if result["ok"] and self._party is not None and self._party.party_name != server_name:
            self._party.rename_server(self._party.host_id, server_name)
        return result

    def _start(self, name, port, select):
        if self._party is not None:
            return {"ok": True, "message": "Your server is already running."}
        if self._store is None:
            return {"ok": False, "message": self._secure_error or "Enter the password for your saved chats first."}
        name = clean_name(name)
        if not valid_port(port):
            return {"ok": False, "message": "Port must be a number from 1024 to 65535."}
        addresses = self._addresses()
        if not addresses:
            return {"ok": False, "message": "No network connection found. Connect the PCs with a cable, a switch, "
                                            "or a phone hotspot, then start again."}
        self._name = name
        self._save(name=name, port=port)
        try:
            certfile, keyfile = ensure_certificate(self._data_dir, ["localhost", "127.0.0.1", *addresses])
        except (OSError, ValueError) as exc:
            log.exception("Could not create the certificate")
            return {"ok": False, "message": f"Could not create the certificate: {plain_error(exc)}"}
        self._cert_pem = Path(certfile).read_text(encoding="utf-8")
        self._certfiles = (str(certfile), str(keyfile))
        passcode = generate_password()  # used only by a brand-new server; an existing one keeps its own
        try:
            if not self._settings.get("server_id"):
                self._save(server_id=secrets.token_hex(8))  # this PC's server keeps one id, so its chat is the same each run
            party = Party(passcode, port, certfile, keyfile, party_name=self._settings.get("server_name") or f"{name}'s server",
                          host_name=name, grabber=self._grabber,
                          theme=self._settings.get("theme", "dark"), store=self._store,
                          server_id=self._settings["server_id"], host_user_id=self._identity.user_id,
                          owner=self._identity)
        except OSError as exc:
            log.exception("Could not listen on port %s", port)
            return {"ok": False, "message": f"Could not listen on port {port}: {plain_error(exc)}"}
        party.start()
        party.friend_relays = self._friend_relays  # members who relay for others, for the invites' relays
        party.relay_listeners.append(lambda: party.net_directory().touch())  # members get new relay settings at once
        self._party = party
        self._passcode = party.passcode
        self._check_reach(party.port)
        if party.invite_secrets():
            self._start_host_punch()
        if self._discovery is not None:
            self._discovery.set_sharing(port, "party", party.fingerprint, party.spki)
        log.info("Server started on port %s", port)
        self._open_own_room(select)  # your server's room, in its place on the rail
        return {"ok": True, "message": "Your server is running."}

    def stop(self):
        if self._party is None:
            return {"ok": True, "message": "Your server is not running."}
        if self._game is not None and self._game["key"] == "own":
            self.game_off()
        self._end_host_punch()
        if "own" in self._room_apis:
            self._shell_room_closed("own")
        self._party.stop()
        self._party = None
        self._passcode = ""
        self._end_reach()
        if self._discovery is not None:
            self._discovery.set_sharing(None, None)
        log.info("Server stopped")
        self.updater.busy_changed()
        return {"ok": True, "message": "Your server is stopped. It stays on your list; start it again any time."}

    def set_server_password(self, password):
        """The owner changes the server's password. Invites made before stop working for new people; everyone who was
        let in before still comes back by who they are."""
        if self._party is None:
            return {"ok": False, "message": "Start your server first."}
        try:
            self._party.set_password(self._party.host_id, password)
        except (Forbidden, NotFound) as exc:
            return {"ok": False, "message": str(exc)}
        self._passcode = self._party.passcode
        return {"ok": True, "message": "Password changed. Copy a new invite for anyone new."}

    def set_relay(self, address, secret, pin=""):
        """The owner sets the group's relay server (relay/README.md): its address (a name or numbers, with :port when it
        is not 3479; or its IPv4 and IPv6 addresses with a comma between), the secret the setup script printed, and its
        certificate fingerprint. Kept with the server; the
        secret never leaves this PC. Returns {ok, message}."""
        if self._party is None:
            return {"ok": False, "message": "Start your server first."}
        parts = [_split_address(part) for part in str(address or "").split(",") if part.strip()] or [("", None)]
        host = ", ".join(h for h, _ in parts)
        port = next((p for _, p in parts if p), None)
        try:
            self._party.set_relay(self._party.host_id, {"host": host, "port": port, "secret": secret, "pin": pin})
        except (Forbidden, NotFound) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "Relay server saved. Members get it the next time they connect."}

    def clear_relay(self):
        if self._party is None:
            return {"ok": False, "message": "Start your server first."}
        self._party.set_relay(self._party.host_id, None)
        return {"ok": True, "message": "Relay server removed."}

    def set_friend_relay(self, on, mbps=None):
        """Allow or stop forwarding for other members who cannot reach each other, and how much at most."""
        changes = {"friend_relay": bool(on)}
        if mbps is not None:
            try:
                mbps = int(mbps)
            except (TypeError, ValueError):
                mbps = 0
            if not valid_mbps(mbps):
                return {"ok": False, "message": "The limit is a whole number of megabits a second, from 1 to 1000."}
            changes["friend_relay_mbps"] = mbps
        self._save(**changes)
        manager = self.path_manager
        if manager is not None:
            manager.set_friend_relay(changes["friend_relay"], changes.get("friend_relay_mbps"))
        return {"ok": True, "message": "Saved." if on else "This PC will not relay for others."}

    def path_settings(self):
        """What a PathManager (pathway.py) on this PC is given as its settings."""
        return {"friend_relay": self._settings.get("friend_relay") is True,
                "friend_relay_mbps": self._settings.get("friend_relay_mbps", 8)}

    def _friend_relays(self):
        manager = self.path_manager
        try:
            return manager.friend_offers() if manager is not None else []
        except Exception:
            log.exception("Reading the members who relay for others failed")
            return []

    def _name_of(self, user_id):
        party = self._party
        for member in (party.roster() if party is not None else []):
            if member.get("user") == user_id:
                return member["name"]
        names = getattr(self.path_manager, "names", None) or {}
        return names.get(user_id) or "a member"

    def _paths_view(self):
        """Who is going through this PC now, and whom this PC cannot reach and why (from the game network's paths)."""
        from privatepartynet.pathway import WAY_TEXT

        manager = self.path_manager
        if manager is None:
            return {"through": [], "problems": [], "ways": []}
        try:
            state, problems = manager.state(), manager.problems()
        except Exception:
            log.exception("Reading the paths failed")
            return {"through": [], "problems": [], "ways": []}
        ways = sorted(f"{self._name_of(peer)}, {WAY_TEXT.get(info.get('kind') or '', 'not connected yet')}"
                      for peer, info in (state.get("peers") or {}).items())
        return {"through": [self._name_of(u) for u in state.get("through") or []],
                "problems": [f"{self._name_of(p['peer'])}: {p['why']}" for p in problems], "ways": ways}

    def rename(self, name):
        name = clean_name(name)
        self._name = name
        self._save(name=name)
        if self._discovery is not None:
            self._discovery.set_name(name)
        if self._party is not None:
            self._party.rename(self._party.host_id, name)
        return {"ok": True, "message": ""}

    def open_room(self):
        """Show your server's room (in its place on the rail)."""
        if self._party is None or self._shell is None:
            return {"ok": False, "message": "Start your server first."}
        self._open_own_room(select=True)
        return {"ok": True, "message": ""}

    def ask_join(self, peer_id, password=""):
        """Ask a nearby PC to let you in. A PC with a party open is asked over HTTPS with your signed identity, checked
        against the certificate its beacon names, so the host knows it is really you and a returning member keeps their
        role. A PC with no party open is asked over the network beacon, and starts one if its person says yes.
        `password` is the party's, for a party that wants one (seen over a VPN, a PC counts as outside the network)."""
        if self._discovery is None:
            return {"ok": False, "message": "Nearby PCs are unavailable on this PC."}
        peer = next((p for p in self._discovery.peer_list() if p["id"] == peer_id), None)
        if peer is None:
            return {"ok": False, "message": "That PC is no longer in the list."}
        if peer.get("sharing") and peer.get("port") and (peer.get("fp") or peer.get("spki")):
            invite = {"name": peer["name"], "password": str(password or "")[:200],
                      "candidates": [["lan", peer["address"], peer["port"]]], "fingerprint": peer.get("fp") or "",
                      "spki": peer.get("spki") or "", "server_id": "", "host_sign": peer.get("sign") or "",
                      "source": "nearby"}
            result = self._begin_join(invite, peer=peer_id)
            if result["ok"]:
                result["message"] = "Asked. Waiting for the other person to say yes."
            return result
        if self._discovery.request(peer_id):
            return {"ok": True, "message": "Asked. Waiting for the other person to say yes."}
        return {"ok": False, "message": "That PC is no longer in the list."}

    def decide(self, request_id, accept):
        if str(request_id).startswith("door:") and self._party is not None:
            try:
                self._party.decide_request(self._party.host_id, str(request_id)[len("door:"):], bool(accept))
            except (Forbidden, NotFound) as exc:
                return {"ok": False, "message": str(exc)}
            return {"ok": True, "message": "Let in." if accept else "Said no."}
        request = self._requests.pop(request_id, None)
        if request is None:
            return {"ok": False, "message": "That request has expired."}
        if not accept:
            self._discovery.answer(request_id, False)
            return {"ok": True, "message": f"Said no to {request['name']}."}
        if self._party is None:
            result = self.start(self._name, self._settings["port"])
            if not result["ok"]:
                self._discovery.answer(request_id, False)
                return result
        token = self._party.join_token(request["name"])
        url = f"https://{address_for(request['address'])}:{self._party.port}/join?token={token}"
        self._discovery.answer(request_id, True, url, cert=self._cert_pem, embed=True, server=self._party.server_id)
        return {"ok": True, "message": f"{request['name']} is joining your party."}

    def set_theme(self, theme):
        if theme not in THEMES:
            return {"ok": False, "message": "Pick Match Windows, Light or Dark."}
        self._save(theme=theme)
        if self._party is not None:
            self._party.set_theme(theme)
        return {"ok": True, "message": ""}

    def appearance(self):
        """This person's accent color (None for the default blue), text size in percent and spacing."""
        return {"accent": self._settings.get("accent"), "text_scale": self._settings.get("text_scale", 100),
                "density": self._settings.get("density", "default")}

    def sounds(self):
        """This person's sounds: {"volume": 0 to 100, "off": the groups turned off}."""
        return {"volume": self._settings.get("sound_volume", 60), "off": list(self._settings.get("sounds_off") or [])}

    def set_sounds(self, volume, off):
        if (not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100 or not isinstance(off, list)
                or any(group not in SOUND_GROUPS for group in off)):
            return {"ok": False, "message": "Those sound settings could not be saved."}
        self._save(sound_volume=volume, sounds_off=sorted(set(off)))
        return {"ok": True, "message": ""}

    def set_appearance(self, accent, text_scale, density):
        """Save how every window looks on this PC. The page has already applied it; this keeps it for next time."""
        accent = accent.lower() if isinstance(accent, str) and accent else None
        if not valid_accent(accent) or not valid_text_scale(text_scale) or density not in DENSITIES:
            return {"ok": False, "message": "That appearance could not be saved."}
        self._save(accent=accent, text_scale=text_scale, density=density)
        return {"ok": True, "message": ""}

    def open_data_folder(self):
        self._data_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(self._data_dir)
        return {"ok": True, "message": ""}

    def _remember_accepted(self, data):
        """A Nearby PC let you in over the network beacon: its server goes on your list like any other."""
        if self._store is None:
            return None
        try:
            whole, spki = pins_of_pem(data["cert"]) if data.get("cert") else ("", "")
        except ValueError:
            whole, spki = "", ""
        parts = urlsplit(data["url"])
        invite = {"name": data.get("name") or "Server", "password": "", "fingerprint": whole, "spki": spki,
                  "server_id": data.get("server") or "", "host_sign": "",
                  "candidates": [["lan", parts.hostname, parts.port or DEFAULT_PORT]] if parts.hostname else []}

        class _Seen:
            seen_fingerprint, seen_spki, tunnel = whole, spki, None

        return self._remember_server(invite, _Seen)

    def _join(self, data):
        """Show a server's room in its place on the rail, in the one window. It accepts only the host's certificate:
        `data["pins"]` (what the join checked) or, from a Nearby accept, the pins of `data["cert"]`. `data["key"]` is the
        server's key on the list. A host too old to open inside the one window gets a note in its place, never a window
        of its own (K, November 5th: "be sure that PPN never exists in more than one window")."""
        pins = tuple(data.get("pins") or ())
        if not pins and data.get("cert"):
            try:
                pins = pins_of_pem(data["cert"])
            except ValueError:
                pins = ()
        parts = urlsplit(data["url"])
        origin = f"{parts.scheme}://{parts.netloc}"
        key = data.get("key") or f"join:{origin}"
        if self._shell is None:
            log.warning("No window to show %s in", data["name"])
            return
        # A host on 3.2.0 or older cannot sign in inside a frame; its room then says nothing, and the rail shows a card in
        # its place after a while (appshell.js notReady). `embed` cannot tell those hosts apart: 3.3.0 hosts left it out of
        # a returning member's answer, and their rooms open here fine.
        windowpins.pin(data["url"], pins)
        token = self._feed.issue()
        separator = "&" if "?" in data["url"] else "?"
        feed = quote(f"http://127.0.0.1:{self._feed.port}", safe="")
        url = f"{data['url']}{separator}feed={feed}&feed_token={token}"
        theme = self._settings.get("theme", "dark")
        if theme != "system":
            url += f"&theme={theme}"
        self._rail_note.pop(key, None)
        self._shell_room(key, data["name"], url, origin, select=data.get("select", True), store=lambda: self._store)
        log.info("Joined %s", data["name"])

    # --- the Info menu ----------------------------------------------------------------------------------------

    def about_info(self):
        """Everything the About page shows. Also used by the How page for the name and version."""
        return {
            "name": APP_NAME,
            "version": __version__,
            "theme": self._settings.get("theme", "dark"),
            "appearance": self.appearance(),
            "webview2": webview2_version() or "not found",
            "python": platform.python_version(),
            "windows": platform.platform(),
            "packaged": bool(getattr(sys, "frozen", False)),
            "app_folder": str(Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent),
            "data_folder": str(self._data_dir),
            "log_file": str(self._data_dir / LOG_FILE),
            "libraries": [{"name": label, "version": library_version(dist), "purpose": purpose} for label, dist, purpose in CREDITS],
            "update": self.updater.view(),
        }

    def open_about(self):
        return self._open_info("about", f"About {APP_NAME}", 520, 700)

    def open_how(self):
        return self._open_info("how", f"How {APP_NAME} works", 760, 820)

    # --- updates ---------------------------------------------------------------------------------------------

    def _update_busy(self):
        """What an update must wait for, in words for the control panel, or "" when nothing is going on."""
        if self._party is not None:
            return "your server"
        if self._party_windows:
            return "a server you are in (and any call in it)"
        if self._game is not None:
            return "the game network"
        return ""

    def _quit_for_update(self):
        """The installer (or the portable swap) is running: close every window so it can replace the app."""
        for window in list(webview.windows):
            try:
                window.destroy()
            except Exception:
                log.debug("A window would not close for the update", exc_info=True)

    def hidden_message(self, where, kind):
        """The control panel did not show a message because it looked like a certificate, key or token (safetext.js).
        Only which box and what kind are logged, from fixed lists; never the message."""
        where = where if where in _HIDING_BOXES else "another box"
        kind = kind if kind in _HIDDEN_KINDS else "a secret"
        log.warning("The control panel hid a message that looked like %s (in the %s)", kind, where)
        return {"ok": True}

    def update_state(self):
        return self.updater.view()

    def check_updates(self):
        """The Check for updates button: asks GitHub now, whatever the automatic setting."""
        return self.updater.check(manual=True)

    def update_restart(self):
        return self.updater.restart_now()

    def update_later(self):
        return self.updater.later()

    def update_skip(self):
        return self.updater.skip()

    def set_update_option(self, name, on):
        """Automatic updates, or Include test versions, on or off."""
        if name not in ("auto_update", "update_prerelease"):
            return {"ok": False, "message": "There is no such option."}
        self._save(**{name: bool(on)})
        return {"ok": True, "message": "", "update": self.updater.view()}

    def _open_info(self, page, title, width, height):
        """About and How open over the one window; direct messages are a page of Home."""
        if page == "dm":
            self._shell_queue({"type": "home", "page": "messages"})
        else:
            self._shell_queue({"type": "info", "page": page, "title": title})
        return {"ok": True, "message": ""}

    def shutdown(self, *_):
        """Called when the control window closes. Ends the party and says goodbye to the network."""
        self.stop()
        self._feed.stop()
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        self._end_reach()
        self.game_off()
        self._end_host_punch()
        for tunnel in self._tunnels.values():
            tunnel.close()
        self._tunnels = {}
        if self._joiner is not None:
            self._joiner.cancel()
        for joiner in self._connecting.values():
            joiner.cancel()
        self._connecting = {}
        if self._dm is not None:
            self._dm.stop()
            self._dm = None
        if self._store is not None:
            self._store.close()
            self._store = None
        if self._shell is not None:
            self._shell.stop()
        self.updater.stop()
        self.updater.on_exit()  # a checked update waiting for the app to close installs now, without starting it again

    # --- helpers ------------------------------------------------------------------------------------------

    def _save(self, **changes):
        self._settings.update(changes)
        save_settings(self._settings, self._data_dir)




def _split_address(text):
    """(host, port) from "relay.example.net", "203.0.113.5:4000" or "[2001:db8::1]:4000"; the port is None if not given."""
    text = str(text or "").strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:
        host, port = text, ""
    return host, (port or None)


def plain_error(exc):
    """What went wrong, for the control panel: an OS error by its reason alone (no "[WinError 10048]" in front, no
    exception name), anything else by the words it carries. The full exception goes to the log, not here."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror.rstrip(".") + "."
    text = str(exc).strip()
    return text if text else "something unexpected went wrong; the details are in the log"


def window_test(seconds=90):
    """The one window, in a hidden window of the real window stack (pywebview, pythonnet, WebView2), checked step by step:
    the shell and its bridge; Home (the control panel) using that bridge from its frame; your own server's room signing in
    inside a frame and asking the app for things through the shell; an HTTPS room pinned to the server's certificate
    signing in inside a frame (the certificate check and the frame's cookie, which only real WebView2 can show); and an
    HTTPS room pinned to another certificate being stopped in its own place while the shell stays.
    Run as: PrivatePartyNet.exe --window-test. Returns 0 when every step passed, 1 otherwise; each step is in the log.
    The build runs it on the packaged app. Nothing shows on screen."""
    import shutil
    import tempfile
    import threading

    folder = tempfile.mkdtemp(prefix="privatepartynet-window-test-")
    controller = Controller(data_folder=folder, discovery_port=0)
    shell = ShellServer().start()
    controller.use_shell(shell)
    window = webview.create_window("PrivatePartyNet", url=shell.url(), js_api=controller, hidden=True,
                                   width=1280, height=820)
    controller.bind_window(window)
    steps = []

    def wait(win, script, limit=25.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                value = win.evaluate_js(script)
                if value:
                    return value
            except Exception:  # the page is still starting
                pass
            time.sleep(0.4)
        return None

    def step(name, ok):
        steps.append((name, bool(ok)))
        log.info("Window test: %s: %s", name, "ok" if ok else "FAILED")
        return bool(ok)

    def server(win, key):
        return (f"(function () {{ var s = window.PPNShell && window.PPNShell.servers().filter(function (x) {{ "
                f"return x.key === {json.dumps(key)}; }})[0]; return s ? JSON.stringify(s) : null; }})()")

    def probe(win):
        try:
            if not step("the shell and its bridge", wait(win, "!!(window.PPNShell && window.pywebview && window.pywebview.api "
                                                             "&& typeof window.pywebview.api.shell_state === 'function')")):
                return
            step("Home uses the window's bridge from its frame", wait(win, (
                "(function () { try { var w = document.getElementById('view-home').contentWindow; return w.pywebview === "
                "window.pywebview && w.document.documentElement.classList.contains('in-shell') && "
                "!!w.document.getElementById('name').value; } catch (e) { return false; } })()")))
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_port:
                probe_port.bind(("127.0.0.1", 0))
                port = probe_port.getsockname()[1]
            started = controller.start("Window test", port)
            if not step("a server starts", started.get("ok")):
                return
            own = wait(win, server(win, "own") + " && JSON.parse(" + server(win, "own") + ").ready && JSON.parse(" +
                       server(win, "own") + ").calls > 0")
            step("its room signs in inside a frame and asks the app through the shell", own)
            party = controller._party
            pins = pins_of_pem(controller._cert_pem)
            good = f"https://127.0.0.1:{party.port}/join?token={party.join_token('Pinned guest')}"
            key = "joined-pinned"
            controller._join({"url": good, "name": "Pinned", "pins": pins, "embed": True, "key": key})
            step("an HTTPS room with the right certificate signs in inside a frame",
                 wait(win, f"(function () {{ var s = {server(win, key)}; return !!s && JSON.parse(s).ready; }})()", 40))
            wrong = f"https://127.0.0.2:{party.port}/join?token={party.join_token('Wrong guest')}"
            bad = "joined-wrong"
            controller._join({"url": wrong, "name": "Wrong", "pins": ("0" * 64,), "embed": True, "key": bad})
            step("an HTTPS room with another certificate is stopped in its own place",
                 wait(win, f"(function () {{ var s = {server(win, bad)}; return !!s && JSON.parse(s).stopped && "
                           f"!JSON.parse(s).ready; }})()", 40))
            step("and the window is still the shell", wait(win, "!!window.PPNShell && !!document.getElementById('servers')"))
        finally:
            win.destroy()

    watchdog = threading.Timer(seconds + 30, lambda: os._exit(2))  # a window that never closes must not hang the build
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(probe, window, **webview_options(Path(folder) / "webview"))
    finally:
        watchdog.cancel()
        controller.shutdown()
        windowpins.forget_all()
        shutil.rmtree(folder, ignore_errors=True)
    passed = len(steps) == 7 and all(ok for _, ok in steps)
    log.info("Window test: %s (%d of 7 steps passed)", "passed" if passed else "FAILED", sum(ok for _, ok in steps))
    window_test.steps = steps
    return 0 if passed else 1


def exit_now(code):
    """End the process once the window stack has stopped and everything is shut down. pywebview answers a page's last
    bridge calls from threads of its own, and one reaching into .NET while Python shuts down crashed the process (an
    access violation, exit 127; more often since party windows check certificates, windowpins.py). Ending here, with the
    log written out, leaves nothing to race."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # pythonw has no console
            pass
    os._exit(code)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    configure_logging()
    # Each party window accepts only the certificate its join checked (windowpins.py), never any certificate at all.
    arguments = browser_arguments(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))
    if arguments:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = arguments
    else:
        os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    windowpins.install()
    if "--check-capture" in argv:
        from privatepartynet.frames import check_capture

        return check_capture()
    if "--smoke-test" in argv:
        from privatepartynet.smoke import run as smoke_run

        return smoke_run()
    if "--window-test" in argv:
        exit_now(window_test())
    sys.excepthook = report_crash
    if not acquire_single_instance():
        log.info("A second copy was started; it asked the first copy to show its window and closed")
        ask_first_copy_to_show()
        return 0
    log.info("PrivatePartyNet %s starting", __version__)
    controller = Controller()
    # One window, as Discord has: the rail of servers, Home (the control panel) and every room in frames beside it.
    shell = ShellServer().start()
    controller.use_shell(shell)
    webview.settings["ALLOW_DOWNLOADS"] = True  # a file in the chat is saved with Windows' Save dialog
    window = webview.create_window(
        "PrivatePartyNet", url=shell.url(), js_api=controller,
        width=1280, height=820, min_size=(900, 600),
    )
    controller.bind_window(window)
    window.events.closed += controller.shutdown
    stop_watching = threading.Event()
    window.events.closed += lambda *_: stop_watching.set()

    def started():
        threading.Thread(target=watch_for_second_copy, args=(window, stop_watching), name="privatepartynet-show",
                         daemon=True).start()
        controller.start_saved()  # your server and your list of servers, as they were

    webview.start(started, **webview_options())
    log.info("PrivatePartyNet closed")
    exit_now(0)
