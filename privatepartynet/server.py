"""HTTPS server for a PrivatePartyNet party: members, group chat, screen-share signaling, and the pages.

Screen and video never pass through this server. Members' browsers connect to each other with WebRTC, and this
server only relays their connection setup (offers, answers, and receive preferences). Chat and the roster go
through here, so every member sees the same list in the same order.
"""

import hashlib
import hmac
import html
import ipaddress
import http.cookies
import logging
import json
import queue
import re
import secrets
import shutil
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from privatepartynet.cpu import sample_detail as cpu_detail
from privatepartynet.discovery import clean_name
from privatepartynet.frames import clamp_fps, clamp_height
from privatepartynet import gamenet
from privatepartynet.hangups import HangupsLogged, describe, is_hangup
from privatepartynet import linkstate
from privatepartynet import relayproto
from privatepartynet.identity import user_id_of, verify
from privatepartynet.paths import web_dir
from privatepartynet.vault import VaultError

WEB_DIR = web_dir()
STATIC_TYPES = {
    "rtc.js": "application/javascript; charset=utf-8",
    "layout.js": "application/javascript; charset=utf-8",
    "netstats.js": "application/javascript; charset=utf-8",
    "pingview.js": "application/javascript; charset=utf-8",
    "capturesize.js": "application/javascript; charset=utf-8",
    "link.js": "application/javascript; charset=utf-8",
    "room.js": "application/javascript; charset=utf-8",
    "chat.js": "application/javascript; charset=utf-8",
    "voice.js": "application/javascript; charset=utf-8",
    "icons.js": "application/javascript; charset=utf-8",
    "shell.js": "application/javascript; charset=utf-8",
    "panels.js": "application/javascript; charset=utf-8",
    "appearance.js": "application/javascript; charset=utf-8",
    "roombridge.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "theme.css": "text/css; charset=utf-8",
    "ui.css": "text/css; charset=utf-8",
}
# Cookie names. Each party adds its server id (Party.cookie_name): the app's one window keeps every server's rooms in one
# cookie jar, cookies are not kept apart by port, and two servers reached on one address (two outside servers, each
# through its own tunnel on 127.0.0.1) would otherwise overwrite each other's sign-in.
MEMBER_COOKIE = "privatepartynet_member"
HOST_COOKIE = "privatepartynet_host"
LOOPBACK = "127.0.0.1"
MAX_MEMBERS = 16
SHARE_SLOTS = 4  # screens one person can share at once; keep in step with SLOTS in web/rtc.js
MAX_LABEL = 40
JOIN_SECONDS = 120  # a join link works once, for this long  # one party, one LAN: a full mesh of screen connections stays manageable at this size
MAX_CHAT = 500
HISTORY = 200  # messages per channel sent to a member who connects; older ones are paged in
PAGE = 50  # messages per page when a member scrolls back
DEFAULT_CHANNEL = "general"
MAX_CHANNELS = 30
MAX_CHANNEL_NAME = 32
MAX_SERVER_NAME = 40
MAX_VOICE = 6  # people in one voice channel: a full mesh of audio and video connections stays practical at this size
STAFF = ("host", "admin")  # the owner (internally "host") and the admins
ROLE_CHOICES = ("admin", "member", "guest")  # what the owner can give; "host" is the owner
MAX_PICTURE = 2 * 1024 * 1024  # a server's icon or a person's picture (shown small, kept at its own size)
MAX_PICTURE_TEXT = "2 MB"
REQUEST_SECONDS = 300  # a join request waits this long for the owner or an admin
MAX_IMAGE = 12 * 1024 * 1024  # an image on the wire: the sender's page saves the full-size picture to fit under this
MAX_IMAGE_TEXT = f"{MAX_IMAGE // (1024 * 1024)} MB"
MAX_MEMORY_BLOBS = 96 * 1024 * 1024  # pictures kept in memory when there is no store (tests, a throwaway party)
IMAGES_PER_MINUTE = 10  # per member
MAX_CONNECTIONS = 128  # requests served at once; each member holds one open event stream, so this is several per person
ASK_FAILS_PER_ADDRESS = 5  # wrong passwords from one address before it is shut out for ASK_LOCKOUT_SECONDS
ASK_LOCKOUT_SECONDS = 900
ASK_FAILS_PER_MINUTE = 30  # wrong passwords from everywhere together before the door closes for a minute
CHALLENGE_SECONDS = 120
MAX_CHALLENGES = 256
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read aloud and typed without mistakes
MAX_PASSWORD = 64
MAX_REQUESTS = 20  # people waiting to be let in at once; more are refused until some are answered
MAX_REQUESTS_PER_ADDRESS = 3
MAX_FORM_BYTES = 1024
MAX_BODY_BYTES = 256 * 1024  # an SDP with every candidate is a few kilobytes
# Files of any kind (store.py keeps them in sealed pieces, never whole in memory). Each server has its own per-file limit
# and storage cap, each None (none, the default) or bytes; when both are set the cap is at least CAP_TIMES_LIMIT times the
# limit (K, 2026-11-04). Pictures count toward both. The host's disk keeps MIN_FREE_BYTES free whatever they say.
FILE_SETTING_MIN = 1024 * 1024
FILE_SETTING_MAX = 64 * 1024 ** 4
CAP_TIMES_LIMIT = 10
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_FILE_NAME = 200
FILES_PER_MINUTE = 20  # per member
UPLOAD_PIECE = 256 * 1024  # an upload is read from the connection this much at a time
# Said in every "allowed" join answer: this party's room signs in inside the app's one window (its HTTPS cookie is
# SameSite=None, server.py _cookie_flags). Hosts on 3.2.0 and older never say it; their rooms open in a window of their own.
EMBED = 1
MAX_DRAIN = 1024 * 1024  # what is read of a refused upload so the answer reaches the client; past it the connection closes
SIGNAL_KINDS = {"offer", "answer", "quality"}
TURN_PORT = 3478  # coturn beside the relay server (relay/turnserver.conf)
TURN_SECONDS = 24 * 3600  # how long the TURN credentials a page is given last; a page that reconnects gets new ones
GO_LIVE_QUALITIES = ("source", "1080", "720", "480")
GO_LIVE_FPS = (15, 30, 60)
MIN_SIZE, MAX_SIZE = 64, 16384
QUALITY_PERCENTS = (100, 75, 50, 25)
HEX_DIGITS = frozenset("0123456789abcdef")
TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"
_STOP = object()


def _pick_source(grabber, monitor_raw):
    """The grabber for the screen asked for, or None when there is no such screen."""
    if monitor_raw is None:
        return grabber
    try:
        monitor = int(monitor_raw)
    except ValueError:
        return None
    listed = grabber.monitors() if hasattr(grabber, "monitors") else []
    return grabber.for_monitor(monitor) if monitor in {m["index"] for m in listed} else None


def _is_size(value):
    return isinstance(value, int) and not isinstance(value, bool) and MIN_SIZE <= value <= MAX_SIZE


def _is_slot(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < SHARE_SLOTS


def clean_label(value):
    """A share's name, printable and short. Falls back to Screen when nothing usable is left."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:MAX_LABEL] or "Screen"


def clean_signal(message):
    """The signal to relay, reduced to the fields its kind needs. None when it is not allowed."""
    if not isinstance(message, dict) or not isinstance(message.get("to"), str):
        return None
    kind = message.get("kind")
    if kind not in SIGNAL_KINDS:
        return None
    if kind in {"offer", "answer"}:
        if not isinstance(message.get("sdp"), str):
            return None
        return {"kind": kind, "sdp": message["sdp"]}
    slot = message.get("slot")
    if not _is_slot(slot):
        return None
    if message.get("mode") == "off":  # the viewer is not showing this screen
        return {"kind": "quality", "slot": slot, "mode": "off"}
    if message.get("mode") == "percent" and message.get("percent") in QUALITY_PERCENTS:
        return {"kind": "quality", "slot": slot, "mode": "percent", "percent": message["percent"]}
    if message.get("mode") == "fit" and _is_size(message.get("width")) and _is_size(message.get("height")):
        return {"kind": "quality", "slot": slot, "mode": "fit", "width": message["width"], "height": message["height"]}
    return None


def clean_chat(value):
    """Chat text: printable characters, trimmed, 1 to MAX_CHAT long. None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable() or ch == " ").strip()
    return text[:MAX_CHAT] or None


def clean_channel_name(value):
    """A channel's name: printable, one line, up to MAX_CHANNEL_NAME. None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch for ch in value if ch.isprintable()).split())
    return text[:MAX_CHANNEL_NAME] or None


def sniff_image(data):
    """The type of an image from its first bytes ("image/png", "image/jpeg", "image/gif", "image/webp"), or None.
    What a sender claims is never trusted; SVG and everything else is refused."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def generate_password():
    """A server password: ten characters in two groups of five, about 49 bits, easy to read out."""
    picks = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(10))
    return picks[:5] + "-" + picks[5:]


def clean_password(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not 6 <= len(text) <= MAX_PASSWORD or not text.isprintable():
        return None
    return text


def net_message(fingerprint, nonce, key):
    """What a member signs to open their game network link: the host certificate they checked, a fresh challenge, and the
    WireGuard public key they will use on it."""
    return f"PrivatePartyNet net|{fingerprint}|{nonce}|{key}".encode("utf-8")


log = logging.getLogger("privatepartynet.server")

# What a joining app can do, as it says in POST /ask ("caps"). "link": it keeps its party window's link through a
# dropout and resumes with a signed token (3.3 on). A member whose app does not say so gets 3.2's behaviour: no
# heartbeat, no grace, no resume, offline the moment their connection closes.
CAPS = frozenset({"link"})


def clean_caps(caps):
    return frozenset(c for c in (caps if isinstance(caps, (list, tuple, set, frozenset)) else ()) if c in CAPS)


def resume_message(fingerprint, nonce, token):
    """What a member signs to come back after a dropout (POST /resume), bound to this host's certificate."""
    return f"PrivatePartyNet resume|{fingerprint}|{nonce}|{token}".encode("utf-8")


def ask_message(fingerprint, nonce, name):
    """What a joiner signs to prove who they are: bound to the host certificate they checked (so a fake host cannot pass
    the signature on to the real one), to a single-use challenge, and to the name they ask under."""
    return f"PrivatePartyNet ask|{fingerprint}|{nonce}|{name}".encode("utf-8")


TUNNEL_SOURCES = ipaddress.ip_network("127.1.0.0/16")  # keep in step with punch.TUNNEL_NETWORK
LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # home and office networks (RFC 1918)
    "127.0.0.0/8", "169.254.0.0/16",  # this PC, and link-local
    "::1/128", "fe80::/10", "fc00::/7"))  # the same for IPv6 (loopback, link-local, unique-local)


def is_local_address(address):
    """True for a request from this PC or this network. Listed networks only: Python's own is_private also counts the
    documentation and benchmarking ranges, and a carrier-grade NAT address (100.64.0.0/10) is shared with strangers."""
    try:
        ip = ipaddress.ip_address(str(address).split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip in TUNNEL_SOURCES:
        return False  # a joiner from outside, carried in through the punch-through tunnel (punch.py)
    return any(ip in network for network in LOCAL_NETWORKS if network.version == ip.version)


def clean_relay(config):
    """The owner's relay server settings, checked: {"host", "port", "tls_port", "pin", "secret", "turn"}. Raises
    ValueError, in words, for anything that cannot be right."""
    if not isinstance(config, dict):
        raise ValueError("The relay settings were not understood.")
    hosts = [h.strip().strip("[]") for h in str(config.get("host") or "").split(",")]
    if not 1 <= len(hosts) <= 2 or any(not h or len(h) > 253 or any(c.isspace() or c in "/@?#" for c in h) for h in hosts):
        raise ValueError("Enter the relay server's address (a name like relay.example.net, or numbers; its IPv4 and "
                         "IPv6 addresses may both be given, with a comma between them).")
    host = ", ".join(hosts)
    try:
        port = int(config.get("port") if config.get("port") not in (None, "") else relayproto.DEFAULT_UDP_PORT)
        tls_port = int(config.get("tls_port") if config.get("tls_port") not in (None, "") else relayproto.DEFAULT_TLS_PORT)
    except (TypeError, ValueError):
        raise ValueError("The relay's ports must be numbers.") from None
    if not 0 < port < 65536 or not 0 <= tls_port < 65536:
        raise ValueError("The relay's ports must be from 1 to 65535.")
    secret = str(config.get("secret") or "").strip()
    if len(secret) < 16 or len(secret) > 200:
        raise ValueError("The relay secret is 16 to 200 characters (the setup script prints it).")
    pin = str(config.get("pin") or "").strip().lower().replace(":", "")
    if pin and (len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin)):
        raise ValueError("The certificate fingerprint is 64 letters and digits (the setup script prints it), or empty.")
    return {"host": host, "port": port, "tls_port": tls_port, "pin": pin, "secret": secret,
            "turn": config.get("turn", True) is not False}


def relay_hosts(relay):
    """The relay server's addresses: one name, or its IPv4 and IPv6 addresses when the owner gave both."""
    return [h.strip() for h in relay["host"].split(",") if h.strip()]


def certificate_fingerprint_file(certfile):
    return certificate_pins_file(certfile)[0]


def certificate_pins_file(certfile):
    """(whole-certificate SHA-256, public-key SHA-256) of the first certificate in `certfile`."""
    from privatepartynet.cert import pins_of_pem

    text = Path(certfile).read_text(encoding="utf-8")
    return pins_of_pem(text[:text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")])


def default_channels():
    return [{"id": DEFAULT_CHANNEL, "name": "general", "kind": "text"},
            {"id": "voice-1", "name": "Voice 1", "kind": "voice"}]


class Forbidden(Exception):
    """The member is known, and their role does not allow this."""


class NotFound(Exception):
    """The channel, member or request asked for does not exist."""


class BadImage(Exception):
    """The upload is not an image the server accepts, or it is too big."""


class Full(Exception):
    """A voice channel already has MAX_VOICE people in it, or a file would not fit this server's limits or the disk."""


class BadRequest(Exception):
    """A setting that cannot be used, with the reason."""


def human_size(n):
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + " " + unit
    return f"{n} bytes"


def check_file_settings(limit, cap):
    """"" when a per-file limit and a storage cap (each None for none, or bytes) can be used together, else why not."""
    for value, what in ((limit, "The file size limit"), (cap, "The storage cap")):
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or not FILE_SETTING_MIN <= value <= FILE_SETTING_MAX:
            return f"{what} is a size of at least 1 MB, or none."
    if limit is not None and cap is not None and cap < CAP_TIMES_LIMIT * limit:
        return "The storage cap has to be at least ten times the file size limit."
    return ""


def clean_file_name(name):
    """A file's name as shown and offered for saving: no folder part, no control or reserved characters, not too long."""
    if not isinstance(name, str):
        return "file"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c >= " " and c != "\x7f" and c not in '<>:"|?*').strip().rstrip(". ")
    return name[:MAX_FILE_NAME].rstrip(". ") or "file"


def clean_mime(value):
    value = (value or "").strip().lower()[:100] if isinstance(value, str) else ""
    return value if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value) else "application/octet-stream"


def parse_range(header, size):
    """(start, end) for a single "bytes=" Range a file of `size` bytes can answer, end not included; None otherwise."""
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not match or not (match.group(1) or match.group(2)):
        return None
    if not match.group(1):
        tail = int(match.group(2))
        return (max(0, size - tail), size) if tail and size else None
    start = int(match.group(1))
    end = int(match.group(2)) + 1 if match.group(2) else size
    if start >= size or end <= start:
        return None
    return start, min(end, size)


def attachment_header(name):
    """Content-Disposition that always saves, never shows, with the name in plain ASCII and in full UTF-8."""
    plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "file"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


class Party:
    """One party on this PC. Build it, call start(), and call stop() to end it.

    Never call stop() before start(), because shutdown() waits for the serving loop to run.
    """

    def __init__(self, passcode, port, certfile, keyfile, party_name, host_name,
                 failed_login_delay=1.0, heartbeat=15.0, grabber=None, theme="system",
                 store=None, server_id=None, host_user_id=None, trust_local=True, owner=None,
                 reconnect_grace=linkstate.RECONNECT_GRACE, ping_seconds=linkstate.PING_SECONDS,
                 link_seconds=(linkstate.UNSTABLE_SECONDS, linkstate.RECONNECTING_SECONDS), clock=None):
        """`reconnect_grace` (the reconnect_grace_s setting) is how long a member whose connection dropped keeps their
        place, role and voice seat; `ping_seconds` and `link_seconds` (unstable, reconnecting) are linkstate's, smaller
        in tests. `clock` is (monotonic, wall) for tests."""
        self.reconnect_grace = float(reconnect_grace)
        self.ping_seconds = float(ping_seconds)
        self.link_seconds = tuple(link_seconds)
        self._clock = clock or (time.monotonic, time.time)
        self._resume = {}  # resume token -> member id: a member coming back after a dropout, without a new approval
        self._join_resume = {}  # join token -> the resume token its member will get
        self._join_caps = {}  # join token -> what the joining app can do (CAPS)
        self._net_graced = {}  # member id -> (user, key): a game network link that dropped; they stay on the list
        self.trust_local = trust_local  # people on this network ask without the password (they still wait for a yes)
        # Addresses that look local but must not be trusted as such: the router, because some routers hand on
        # connections from the internet under their own address once a port is forwarded.
        self.untrusted = set()
        self.store = store  # the encrypted chat store; None keeps chat in memory for this run only
        self.server_id = server_id or secrets.token_hex(8)
        self._cookie_suffix = "".join(c for c in self.server_id.lower() if c in "0123456789abcdef")[:16] or "party"
        self._host_user_id = host_user_id
        self._signer = owner.sign if owner is not None else None  # the host identity's sign(data); it signs the game network's list of members
        self.theme = theme  # "system", "light" or "dark"; every page the party serves uses it
        self.grabber = grabber  # frames for the host's own screen; None disables the host's frame route
        self.failed_login_delay = failed_login_delay
        self.heartbeat = heartbeat  # seconds between keep-alives; a failed keep-alive is how a dropped browser shows
        self.last_error = ""
        self._members = {}  # id -> record
        self._sessions = {}  # token -> member id
        self._joins = {}  # join token -> (name, expires at, role, user id)
        record = (store.server(self.server_id) if store is not None else None) or {}
        # The server's own name and icon, which the owner and admins change (rename_server, set_icon). `party_name` only
        # names a brand-new server; a saved one keeps its name.
        self.party_name = clean_name(record.get("name") or party_name)
        icon = record.get("icon")
        self._icon = icon if isinstance(icon, str) and len(icon) == 64 and HEX_DIGITS.issuperset(icon) else None
        self._channels = [dict(c) for c in record.get("channels") or default_channels()]
        self._roles = dict(record.get("roles") or {})  # user id -> "admin", "member" or "guest"
        self._banned = set(record.get("banned") or [])
        self._known = dict(record.get("known") or {})  # user id -> {"name", "role", "seen"}: everyone who has joined
        # user id -> the hash of the picture that person's app gave this server (set_avatar); the host's too
        self._avatars = {u: h for u, h in (record.get("avatars") or {}).items()
                         if isinstance(u, str) and isinstance(h, str) and len(h) == 64 and HEX_DIGITS.issuperset(h)}
        self._created = record.get("created") or time.time()
        # The password belongs to the server and is kept with it, so an invite and a remembered server keep working
        # after the host restarts. `passcode` is only the password a brand-new server starts with.
        self.passcode = record.get("password") or passcode
        self.fingerprint = certificate_fingerprint_file(certfile)  # the whole certificate's SHA-256, as 3.0/3.1 pin it
        self.spki = certificate_pins_file(certfile)[1]  # its public key's, which a renewal keeps (cert.py)
        self._challenges = {}  # nonce -> expires at
        self._net_subnet = record.get("net")  # this server's game network, chosen once and kept
        self._net_vips = dict(record.get("vips") or {})
        self._net = None  # the directory of who is on, made when the first person joins the game network
        self._net_links = {}  # member id -> (event that ends their game network link, their id, their WireGuard key)
        self._net_signed = (None, None)  # (version, signed list): the last list signed, sent to everyone
        self._invite_secrets = [dict(i) for i in record.get("invites") or [] if isinstance(i, dict)]
        # The relay server the owner set (relay/README.md), kept with the server in the encrypted store. Its secret stays
        # on this PC: members get tokens and TURN credentials made from it, never the secret.
        self._relay = record.get("relay") if isinstance(record.get("relay"), dict) else None
        files = record.get("files") if isinstance(record.get("files"), dict) else {}
        limit, cap = files.get("limit"), files.get("cap")
        self._file_limit, self._storage_cap = (None, None) if check_file_settings(limit, cap) else (limit, cap)
        self._file_times = {}  # member id -> times of their recent file uploads
        # The owner's identity (identity.Identity): it signs the relay settings members receive and the passes friend
        # relays check (pathway.py). None in tests and diagnostic runs, which then hand out no friend passes.
        self.owner = owner
        self.friend_relays = lambda: []  # (address, port) of members who relay for others, from the app's paths
        self.relay_listeners = []  # called with no arguments when the relay settings change
        self._fails = {}  # address -> times of wrong passwords
        self._fails_all = deque()  # times of wrong passwords from anywhere
        self._log = {}  # channel id -> deque of the newest HISTORY messages, as sent to members
        self._seq = {}  # channel id -> last sequence number
        self._requests = {}  # request id -> a person waiting to be let in
        self._answers = {}  # ticket -> the answer to a request that has been decided, until the person picks it up
        self._last_ts = 0.0
        # True once someone joined from outside this network. From then on every party page gives its peer connections
        # public STUN servers, so screens and voice can reach that person through their router, and the TURN server
        # beside the group's relay when the owner set one, for two routers that let nothing through.
        self.remote = False
        self._blobs = {}  # image hash -> bytes, used only when there is no store
        self._image_times = {}  # member id -> times of their recent uploads
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._host_id = secrets.token_hex(8)
        self._host_token = secrets.token_urlsafe(32)
        self._members[self._host_id] = {
            "id": self._host_id, "name": clean_name(host_name), "role": "host", "user": host_user_id, "voice": None,
            "online": False, "shares": {}, "queue": None,
        }
        self._load_logs()
        self._save_locked()
        self._sessions[self._host_token] = self._host_id
        self._started = self._clock[1]()
        self._pulse_thread = threading.Thread(target=self._pulse_loop, name="privatepartynet-pulse", daemon=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self._httpd = _Server(("0.0.0.0", port), self, secure=True)
        # The handshake runs in the request thread, so one slow client cannot block the accept loop.
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True, do_handshake_on_connect=False)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="privatepartynet-https", daemon=True)
        # The host's own window talks to this PC only, over plain HTTP on the loopback address. Loopback is
        # trusted by browsers, so the page can use the screen-capture APIs without a certificate warning.
        self._local = _Server(("127.0.0.1", 0), self, secure=False)
        self._local_thread = threading.Thread(target=self._local.serve_forever, name="privatepartynet-local", daemon=True)

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def local_port(self):
        return self._local.server_address[1]

    @property
    def closing(self):
        return self._closing.is_set()

    @property
    def host_token(self):
        return self._host_token

    @property
    def member_count(self):
        with self._lock:
            return len(self._members)

    @property
    def host_id(self):
        return self._host_id

    @property
    def host_connected(self):
        with self._lock:
            host = self._members.get(self._host_id)
            return bool(host and host["online"])

    @property
    def viewer_connected(self):
        """True when at least one member other than the host is online."""
        with self._lock:
            return any(m["online"] for m in self._members.values() if m["role"] != "host")

    def start(self):
        self._thread.start()
        self._local_thread.start()
        self._pulse_thread.start()

    def stop(self):
        if self._closing.is_set():
            return
        self._closing.set()
        with self._lock:
            for member in self._members.values():
                if member["queue"] is not None:
                    member["queue"].put({"type": "stop"})
                    member["queue"].put(_STOP)
            self._members.clear()
            self._sessions.clear()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._local.shutdown()
        self._local.server_close()

    # --- members and sessions -------------------------------------------------------------------------------

    def join_token(self, name, role="member", user=None, caps=()):
        """A single-use join token for one person, valid for JOIN_SECONDS. The host's approval hands it to the
        joiner's app, which opens the server in its one window with it. A browser cannot get one.
        `user` is the joiner's permanent id; with it the person keeps their role and cannot join while banned."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._joins[token] = (clean_name(name or "Guest"), time.monotonic() + JOIN_SECONDS, role, user)
            caps = clean_caps(caps)
            if caps:
                self._join_caps[token] = caps
            if user is not None and "link" in caps:
                # Handed to the joiner's app with the answer (GET /ask?ticket=), never to the page: with it, and a
                # signature by the same identity, the member comes back after a dropout without a new approval.
                self._join_resume[token] = secrets.token_urlsafe(32)
        return token

    def _allowed_locked(self, token, until):
        # `embed`: the room can open inside the one window (K, 3.3.0); without it a returning person's app took this
        # for a 3.2.0 host and opened the server in a second window (K, November 5th, on a member's PC)
        answer = {"state": "allowed", "token": token, "embed": EMBED, "until": until}
        if token in self._join_resume:
            answer["resume"] = self._join_resume[token]
        return answer

    def redeem(self, token):
        """Turn a join token into a session token. None when the token is unknown, used or expired, or the server is full."""
        with self._lock:
            entry = self._joins.pop(token, None)
            resume = self._join_resume.pop(token, None)
            caps = self._join_caps.pop(token, frozenset())
            if entry is None or time.monotonic() > entry[1]:
                return None
            name, _, role, user = entry
            if user is not None and user in self._banned:
                return None
            if user is not None:
                role = self._roles.get(user, role)  # a returning person keeps the role they were given
                # a person who comes back replaces their own old, offline entry instead of adding a second one
                for old_id, old in list(self._members.items()):
                    # (an entry still in its reconnect grace has no stream open: the person is back by a new join)
                    gone = not old["online"] or old["queue"] is None
                    if old.get("user") == user and old["role"] != "host" and gone:
                        self._remove_locked(old_id, notice=None)
            if len(self._members) >= MAX_MEMBERS:
                return None
            member_id = secrets.token_hex(8)
            now, wall = self._clock[0](), self._clock[1]()
            self._members[member_id] = {
                "id": member_id, "name": name, "role": role, "user": user, "voice": None,
                "online": False, "shares": {}, "queue": None, "resume": resume, "suspended": False, "leaving": False,
                "caps": caps,
                # a heartbeat only for a member whose app keeps its link through a dropout; the others get 3.2's ways
                "meter": linkstate.LinkMeter(now, wall, *self.link_seconds) if "link" in caps else None,
            }
            if resume:
                self._resume[resume] = member_id
            if user is not None:
                self._known[user] = {"name": name, "role": role, "seen": time.time()}
                self._save_locked()
            session = secrets.token_urlsafe(32)
            self._sessions[session] = member_id
        return session

    def note_join_address(self, address):
        """A person came in from `address`. Someone from outside this network turns STUN on for every page."""
        if is_local_address(address):
            return False
        with self._lock:
            if self.remote:
                return False
            self.remote = True
            self._broadcast_ice_locked()
        return True

    def role_for(self, token):
        """The role of the member behind a session token ("host", "admin", "member" or "guest"), or None."""
        with self._lock:
            member = self._members.get(self._sessions.get(token))
            return member["role"] if member else None
    def member_for(self, token):
        """The member id behind a session token, or None."""
        with self._lock:
            return self._sessions.get(token)

    def attach(self, member_id, since=None):
        """Connect a member's event stream. Returns its queue, or None if the member has gone or must resume first
        (see suspended). `since` ({channel id: last seq seen}) is a page coming back after a dropout: each text channel
        then sends only the messages after that seq, marked "since", which the page adds to what it has."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member.get("suspended"):
                return None
            listener = queue.Queue()
            old = member["queue"]
            member["queue"] = listener
            member["online"] = True
            member["leaving"] = False
            if old is not None:
                old.put(_STOP)  # a second stream for the same member: the first one has been replaced
            if member.get("meter") is not None:
                member["meter"].connected(self._clock[0]())
            listener.put({"type": "hello", "you": self._public(member), "party": self.party_name, "icon": self._icon,
                          "host": self._host_id, "theme": self.theme, "server": self.server_id,
                          "channels": self._channels_public(),
                          "stun": self.remote, "turn": self._turn_locked(member), "storage": self.storage_info(),
                          # "link": this member's place is kept through a dropout, for "grace" seconds (their app said
                          # it can keep its link); without it the page reconnects for as long as it takes, as before 3.5
                          "grace": self.reconnect_grace, "link": member.get("meter") is not None})
            for channel in self._channels:
                if channel["kind"] == "text":
                    listener.put(self._history_since_locked(channel["id"], (since or {}).get(channel["id"])))
            if member["role"] in STAFF:
                for request in self._requests_public_locked():
                    listener.put({"type": "join_request", **request})
            self._broadcast_roster_locked()
            return listener

    def detach(self, member_id, listener):
        """A member's event stream ended. A page that said goodbye (closed, POST /bye) is offline at once; one whose
        connection dropped keeps their place, role, voice seat and screens while the pulse waits out the grace."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["queue"] is not listener:
                return
            member["queue"] = None
            if member["role"] == "host" or member.get("leaving") or self.reconnect_grace <= 0 or member.get("meter") is None:
                self._offline_locked(member)  # (an app without the link: as in 3.2, offline at once)
                return
            log.info("%s's party connection dropped; keeping their place for up to %.0f seconds", member["name"],
                     self.reconnect_grace)

    def _offline_locked(self, member):
        member["online"] = False
        member["shares"] = {}
        member["voice"] = None
        self._end_net_locked(member["id"])  # closing the server's page ends the game network link too
        self._broadcast_roster_locked()

    def bye(self, member_id):
        """The page is closing for good (it said so on its way out): offline at once, no grace."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return False
            member["leaving"] = True
            if member["queue"] is None and member["online"]:
                self._offline_locked(member)
            return True

    # --- the links: heartbeats, dropouts and coming back -------------------------------------------------------

    def _history_since_locked(self, channel_id, since):
        """The history message for one text channel: all of it, or with `since` only what came after that seq."""
        kept = list(self._log.get(channel_id, ()))
        if since is None:
            return {"type": "history", "channel": channel_id, "messages": kept}
        try:
            since = int(since)
        except (TypeError, ValueError):
            return {"type": "history", "channel": channel_id, "messages": kept}
        oldest = kept[0].get("seq", 0) if kept else self._seq.get(channel_id, 0) + 1
        if since + 1 < oldest:
            return {"type": "history", "channel": channel_id, "messages": kept}  # more was missed than is kept: all
        return {"type": "history", "channel": channel_id, "since": since,
                "messages": [m for m in kept if m.get("seq", 0) > since]}

    def pong(self, member_id, ping_id, address=None):
        """A member's page answered a ping. Returns that member's link as everyone sees it, or None."""
        with self._lock:
            member = self._members.get(member_id)
            meter = member.get("meter") if member is not None else None
            if meter is None or not meter.pong(ping_id, self._clock[0]()):
                return None
            if address is not None:
                meter.path = linkstate.path_of(address, linkstate.TUNNEL_WAYS) or meter.path
            if meter.update(self._clock[0](), self._clock[1]()):
                self._link_changed_locked(member)
            return meter.public()

    def link_of(self, member_id):
        """{"state": ...} for a member (the page asks after a dropout), or None when they are no longer in the party.
        A suspended member gets {"resume": True}: their app must sign POST /resume before the page may come back."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return None
            if member.get("suspended"):
                return {"resume": True}
            meter = member.get("meter")
            return meter.public() if meter is not None else {"state": "ok"}

    def suspended(self, member_id):
        with self._lock:
            member = self._members.get(member_id)
            return bool(member and member.get("suspended"))

    def resume(self, token, card, nonce, signature):
        """A member's app brings them back after a dropout: the resume token from their join, and a signature over a
        fresh challenge by the identity they joined with. Returns the member's name. Forbidden when the token is
        unknown (never issued, revoked, or its member was removed after the grace, kicked or banned) or the identity
        is not the member's."""
        try:
            user, sign, dh = card["id"], card["sign"], card["dh"]
            if user_id_of(sign, dh) != user:
                raise ValueError("id")
        except (KeyError, TypeError, ValueError):
            raise Forbidden("Your identity could not be checked.") from None
        with self._lock:
            until = self._challenges.pop(str(nonce), None)
            member = self._members.get(self._resume.get(str(token)))
        if until is None or until < time.monotonic():
            raise Forbidden("The sign-in step expired. Try again.")
        if member is None:
            raise Forbidden("Your place in the party has ended. Ask to join again.")
        if not isinstance(signature, str) or not verify(sign, signature, resume_message(self.fingerprint, nonce, token)):
            raise Forbidden("Your identity could not be checked.")
        with self._lock:
            if member.get("user") != user or user in self._banned or self._members.get(member["id"]) is not member:
                raise Forbidden("Your place in the party has ended. Ask to join again.")
            member["suspended"] = False
            member["meter"].connected(self._clock[0]())
            log.info("%s came back after a dropout", member["name"])
            return member["name"]

    def revoke_resume(self, member_id):
        """The member can no longer come back by their resume token (they must ask to join again after a dropout)."""
        with self._lock:
            member = self._members.get(member_id)
            if member is not None and member.get("resume"):
                self._resume.pop(member["resume"], None)
                member["resume"] = None

    def _link_changed_locked(self, member):
        if member["meter"].state == "reconnecting" and member.get("resume"):
            member["suspended"] = True  # from now on only the member's own identity brings them back
        self._broadcast_roster_locked()

    def _pulse_loop(self):
        while not self._closing.wait(self.ping_seconds):
            try:
                self.pulse()
            except Exception:
                log.exception("The party's heartbeat failed once")

    def pulse(self):
        """Ping every member's page, move each link's state on, and take out whoever stayed away past the grace. Also
        sends everyone the links of all members ({"type": "links"}), so the round trips stay fresh between rosters."""
        now, wall = self._clock[0](), self._clock[1]()
        with self._lock:
            stale = time.monotonic()
            for token in [t for t, entry in self._joins.items() if entry[1] < stale]:
                del self._joins[token]
            for spare in (self._join_resume, self._join_caps):
                for token in [t for t in spare if t not in self._joins]:
                    del spare[token]
            changed = False
            for member in list(self._members.values()):
                meter = member.get("meter")
                if meter is None or member["role"] == "viewer":
                    continue
                if member["queue"] is not None:
                    member["queue"].put({"type": "ping", "id": meter.ping(now)})
                if meter.update(now, wall):
                    changed = True
                    if meter.state == "reconnecting" and member.get("resume"):
                        # Only the member's own identity brings them back now (POST /resume), not the cookie alone.
                        member["suspended"] = True
                    log.info("%s's link is %s", member["name"], meter.state)
                if member["online"] and meter.silent_for(now) >= self.reconnect_grace:
                    log.info("%s did not come back within %.0f seconds; they are out of the party", member["name"],
                             self.reconnect_grace)
                    self._remove_locked(member["id"], notice={"type": "dropped"})
                    changed = False  # _remove_locked sent the roster
            if changed:
                self._broadcast_roster_locked()
            links = {m["id"]: self._link_public(m) for m in self._members.values() if m["role"] != "viewer"}
            self._broadcast_locked({"type": "links", "links": links})

    def leave(self, member_id):
        """A member leaves the party. The host cannot leave; the host stops the party instead."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["role"] == "host":
                return False
            self._remove_locked(member_id, notice={"type": "left"})
            return True

    def _may_remove_locked(self, actor_id, target_id):
        """The owner can remove anyone but themselves. An admin can remove members and guests, not the owner or admins."""
        actor, target = self._members.get(actor_id), self._members.get(target_id)
        if actor is None or target is None or actor["role"] not in STAFF or target_id == actor_id:
            return False
        if target["role"] == "host" or (actor["role"] == "admin" and target["role"] == "admin"):
            return False
        return True

    def kick(self, actor_id, target_id):
        """Remove a member. They may ask again, and then wait for a yes like anyone new (their role is not kept)."""
        with self._lock:
            if not self._may_remove_locked(actor_id, target_id):
                return False
            user = self._members[target_id].get("user")
            self._remove_locked(target_id, notice={"type": "kicked"})
            if user is not None:
                self._known.pop(user, None)
                self._roles.pop(user, None)
                self._save_locked()
            return True

    def ban(self, actor_id, target_id):
        """Remove a person and refuse their id from now on. False when the actor may not, or the person has no id."""
        with self._lock:
            if not self._may_remove_locked(actor_id, target_id):
                return False
            user = self._members[target_id].get("user")
            if user is None:
                return False
            self._banned.add(user)
            self._roles.pop(user, None)
            self._known.pop(user, None)
            self._remove_locked(target_id, notice={"type": "kicked"})
            self._save_locked()
            return True

    def unban(self, actor_id, user):
        with self._lock:
            actor = self._members.get(actor_id)
            if actor is None or actor["role"] not in STAFF or user not in self._banned:
                return False
            self._banned.discard(user)
            self._save_locked()
            return True

    def banned(self):
        with self._lock:
            return sorted(self._banned)

    def set_role(self, actor_id, target_id, role):
        """Give a member a role. The owner can make admins, members and guests; an admin can only switch members and
        guests between those two. The owner's role never changes. Remembered by person."""
        with self._lock:
            actor, target = self._members.get(actor_id), self._members.get(target_id)
            if actor is None or target is None or role not in ROLE_CHOICES:
                raise NotFound("no such member or role")
            if target["role"] == "host" or target_id == actor_id:
                raise Forbidden("that member's role cannot be changed")
            if actor["role"] == "host":
                pass
            elif actor["role"] == "admin":
                if target["role"] == "admin" or role == "admin":
                    raise Forbidden("only the owner can make or change an admin")
            else:
                raise Forbidden("only the owner and admins can change roles")
            target["role"] = role
            if role == "guest":
                target["shares"] = {}
                target["voice"] = None
                self._end_net_locked(target_id)  # guests watch and read; they are not on the game network
            user = target.get("user")
            if user is not None:
                self._roles[user] = role
                self._known.setdefault(user, {"name": target["name"], "seen": time.time()})["role"] = role
                self._save_locked()
            if target["queue"] is not None:
                target["queue"].put({"type": "role", "role": role})
            self._broadcast_roster_locked()
            return True

    # --- who may come in -------------------------------------------------------------------------------------

    def request_join(self, name, user=None, address="", caps=()):
        """Someone outside is asking to join. The owner and every admin online see the request and one of them decides.
        Returns the request id (for the people deciding) and a ticket (the asker's own handle) as (id, ticket)."""
        request_id, ticket = secrets.token_hex(6), secrets.token_urlsafe(16)
        address = str(address)[:64]
        with self._lock:
            self._expire_requests_locked()
            if user is not None and user in self._banned:
                raise Forbidden("this person is banned")
            if len(self._requests) >= MAX_REQUESTS or sum(1 for r in self._requests.values() if r["address"] == address and address) >= MAX_REQUESTS_PER_ADDRESS:
                raise Full("too many people are waiting to be let in; try again in a minute")
            self._requests[request_id] = {"id": request_id, "ticket": ticket, "name": clean_name(name or "Guest"),
                                          "user": user, "address": address, "until": time.monotonic() + REQUEST_SECONDS,
                                          "caps": clean_caps(caps)}
            self._notify_staff_locked({"type": "join_request", **self._requests_public_locked()[-1]})
        return request_id, ticket

    def set_password(self, actor_id, password):
        """The owner changes the server's password. People already let in are not affected: they come back by who they
        are, not by the password."""
        password = clean_password(password)
        with self._lock:
            actor = self._members.get(actor_id)
            if actor is None or actor["role"] != "host":
                raise Forbidden("only the owner can change the password")
            if password is None:
                raise NotFound("a password is 6 to 64 characters")
            self.passcode = password
            self._save_locked()
            return True

    INVITE_DAYS = 30
    MAX_INVITES = 20

    def new_invite_secret(self):
        """A fresh secret for one invite (hex). The host watches the DHT mailbox it names for 30 days (punch.py)."""
        secret = secrets.token_hex(16)
        with self._lock:
            now = time.time()
            self._invite_secrets = [i for i in self._invite_secrets if now - i.get("t", 0) < self.INVITE_DAYS * 86400]
            self._invite_secrets.append({"r": secret, "t": now})
            self._invite_secrets = self._invite_secrets[-self.MAX_INVITES:]
            self._save_locked()
        return secret

    def invite_secrets(self):
        with self._lock:
            now = time.time()
            return [bytes.fromhex(i["r"]) for i in self._invite_secrets if now - i.get("t", 0) < self.INVITE_DAYS * 86400]

    def challenge(self):
        """A single-use value a joiner signs, so the signature cannot be used twice."""
        nonce = secrets.token_hex(16)
        with self._lock:
            now = time.monotonic()
            for old in [n for n, until in self._challenges.items() if until < now]:
                del self._challenges[old]
            if len(self._challenges) >= MAX_CHALLENGES:
                self._challenges.pop(next(iter(self._challenges)))
            self._challenges[nonce] = now + CHALLENGE_SECONDS
        return nonce

    def _prove(self, name, card, nonce, signature):
        """The joiner's permanent id when the card and signature prove it, else raise Forbidden. None without a card."""
        if card is None:
            return None
        try:
            user, sign, dh = card["id"], card["sign"], card["dh"]
            if user_id_of(sign, dh) != user:
                raise ValueError("id")
        except (KeyError, TypeError, ValueError):
            raise Forbidden("Your identity could not be checked.") from None
        with self._lock:
            until = self._challenges.pop(str(nonce), None)
        if until is None or until < time.monotonic():
            raise Forbidden("The sign-in step expired. Try again.")
        if not isinstance(signature, str) or not verify(sign, signature, ask_message(self.fingerprint, nonce, name)):
            raise Forbidden("Your identity could not be checked.")
        return user

    def _shut_out(self, address):
        now = time.monotonic()
        with self._lock:
            while self._fails_all and now - self._fails_all[0] > 60:
                self._fails_all.popleft()
            mine = [t for t in self._fails.get(address, []) if now - t < ASK_LOCKOUT_SECONDS]
            if mine:
                self._fails[address] = mine
            else:
                self._fails.pop(address, None)
            return len(mine) >= ASK_FAILS_PER_ADDRESS or len(self._fails_all) >= ASK_FAILS_PER_MINUTE

    def _failed(self, address):
        now = time.monotonic()
        with self._lock:
            self._fails.setdefault(address, []).append(now)
            self._fails_all.append(now)
            if len(self._fails) > 4096:  # a flood from many addresses must not grow this without end
                self._fails.pop(next(iter(self._fails)))

    def ask(self, name, password, address="", card=None, nonce=None, signature=None, caps=()):
        """The front door for someone who is not in the party yet. Returns the ticket they ask about.

        * Someone the server has let in before, who proves it is them (their card and a signature over a fresh
          challenge), comes straight back in: no password and no waiting, unless they were banned or removed.
        * Anyone else waits for the owner or an admin to say yes. From outside this network they also need the
          password; on this network (when trust_local) they do not.
        * Wrong passwords shut one address out after ASK_FAILS_PER_ADDRESS tries for ASK_LOCKOUT_SECONDS, and shut the
          door for everyone for a minute after ASK_FAILS_PER_MINUTE, so the password cannot be guessed by brute force.
        """
        name = clean_name(name or "Guest")
        address = str(address)[:64]
        if self._shut_out(address):
            raise Full("Too many wrong passwords. Wait a few minutes and try again.")
        user = self._prove(name, card, nonce, signature)
        with self._lock:
            if user is not None and user in self._banned:
                raise Forbidden("You are banned from this server.")
            returning = user is not None and user in self._known
        if returning:
            ticket = secrets.token_urlsafe(16)
            token = self.join_token(name, user=user, caps=caps)
            with self._lock:
                self._answers[ticket] = self._allowed_locked(token, time.monotonic() + JOIN_SECONDS)
            return ticket
        local = self.trust_local and is_local_address(address) and address not in self.untrusted
        given = str(password or "").encode("utf-8")
        if not local and not hmac.compare_digest(given, self.passcode.encode("utf-8")):
            self._failed(address)
            time.sleep(self.failed_login_delay)
            raise Forbidden("That password is not right.")
        return self.request_join(name, user, address, caps=caps)[1]

    def answer(self, ticket):
        """Where a request stands, for the person who asked: {"state": "pending"} while it waits, {"state": "denied"},
        {"state": "allowed", "token": ...} once (the join link's token), or {"state": "gone"} when it expired."""
        with self._lock:
            self._expire_requests_locked()
            if any(r["ticket"] == ticket for r in self._requests.values()):
                return {"state": "pending"}
            answer = self._answers.pop(ticket, None)
            if answer is None:
                return {"state": "gone"}
            return answer

    def decide_request(self, actor_id, request_id, allow):
        """The owner or an admin lets a person in or turns them away. Returns the join token when allowed, else None."""
        with self._lock:
            actor = self._members.get(actor_id)
            if actor is None or actor["role"] not in STAFF:
                raise Forbidden("only the owner and admins can decide who joins")
            self._expire_requests_locked()
            request = self._requests.pop(request_id, None)
            if request is None:
                raise NotFound("that request has already been answered or has expired")
            self._notify_staff_locked({"type": "join_resolved", "id": request_id, "by": actor["name"], "allowed": bool(allow)})
            if not allow:
                self._answers[request["ticket"]] = {"state": "denied", "until": time.monotonic() + REQUEST_SECONDS}
                return None
            token = self.join_token(request["name"], user=request["user"], caps=request.get("caps", ()))
            self._answers[request["ticket"]] = self._allowed_locked(token, time.monotonic() + JOIN_SECONDS)
            return token

    def pending_requests(self):
        with self._lock:
            self._expire_requests_locked()
            return self._requests_public_locked()

    def _expire_requests_locked(self):
        now = time.monotonic()
        for request_id in [r for r, v in self._requests.items() if v["until"] < now]:
            del self._requests[request_id]
        for ticket in [t for t, v in self._answers.items() if v["until"] < now]:
            del self._answers[ticket]

    def _requests_public_locked(self):
        return [{"id": r["id"], "name": r["name"], "user": r["user"], "address": r["address"]}
                for r in self._requests.values()]

    def _notify_staff_locked(self, message):
        for member in self._members.values():
            if member["role"] in STAFF and member["queue"] is not None:
                member["queue"].put(message)

    def rename(self, member_id, name):
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return False
            member["name"] = clean_name(name)
            user = member.get("user")
            if user is not None and user in self._known:
                self._known[user]["name"] = member["name"]
                self._save_locked()
            self._broadcast_roster_locked()
            return True

    def set_share(self, member_id, slot, sharing, label="", width=0, height=0):
        """Start, update or stop one of a member's screens. Each member has SHARE_SLOTS slots, one per screen.

        A screen belongs to the voice channel its sharer is in, and only the people in that channel see it (K, November
        5th: "video sharing should only be possible when you are joined to a voice channel", "video streams and shares
        should be tied specifically to a channel"). So starting one needs a voice channel (Forbidden without), and
        leaving or changing voice channels ends them all (join_voice)."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["role"] == "guest" or not _is_slot(slot):
                return False
            if sharing and not member.get("voice"):
                raise Forbidden("Join a voice channel to share your screen.")
            if sharing:
                member["shares"][slot] = {
                    "label": clean_label(label),
                    "width": width if _is_size(width) else 0,
                    "height": height if _is_size(height) else 0,
                }
            else:
                member["shares"].pop(slot, None)
            self._broadcast_roster_locked()
            return True

    # --- the server's name and icon, and people's pictures ---------------------------------------------------

    def rename_server(self, actor_id, name):
        """The owner or an admin names the server. Everyone's rail and header show the new name at once."""
        name = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()[:MAX_SERVER_NAME]
        if not name:
            raise NotFound("a server needs a name")
        with self._lock:
            self._staff_locked(actor_id, "only the owner and admins can rename the server")
            self.party_name = name
            self._save_locked()
            self._broadcast_server_locked()
            return name

    def set_icon(self, actor_id, data):
        """The owner or an admin gives the server an icon (a picture up to MAX_PICTURE, kept at its own size), or takes it
        away with empty data. Returns the icon's hash, or None."""
        with self._lock:
            self._staff_locked(actor_id, "only the owner and admins can change the server's icon")
        digest = self._keep_picture(data) if data else None
        with self._lock:
            self._icon = digest
            self._save_locked()
            self._broadcast_server_locked()
        return digest

    def icon(self):
        with self._lock:
            return self._icon

    def set_avatar(self, member_id, data):
        """A person's app hands this server their picture (Settings, My profile), or takes it away with empty data. Kept by
        person, so it is there the next time they come in, and shown beside their name everywhere on the server."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not on this server")
            user = member.get("user") or member_id
            now = time.monotonic()
            recent = [t for t in self._image_times.get(member_id, []) if now - t < 60]
            if len(recent) >= IMAGES_PER_MINUTE:
                raise Forbidden("that is a lot of pictures; wait a moment")
            self._image_times[member_id] = recent + [now]
        digest = self._keep_picture(data) if data else None
        with self._lock:
            self._avatars.pop(user, None)
            if digest:
                self._avatars[user] = digest
            self._save_locked()
            self._broadcast_roster_locked()
        return digest

    def _keep_picture(self, data):
        """Keep an icon or a person's picture; returns its hash. Raises BadImage for anything that is not a PNG, JPEG,
        GIF or WebP up to MAX_PICTURE. These are small and do not count toward the server's file storage. A replaced one
        is not deleted: the store keeps pictures by their hash, and the same bytes may be a chat picture, a direct
        message's or this PC's own picture."""
        if len(data) > MAX_PICTURE:
            raise BadImage(f"pictures for icons can be up to {MAX_PICTURE_TEXT}")
        if sniff_image(data) is None:
            raise BadImage("only PNG, JPEG, GIF and WebP pictures can be used")
        digest = hashlib.sha256(data).hexdigest()
        if self.store is not None:
            self.store.put_blob(data)
        else:
            self._blobs[digest] = data
        return digest

    def _broadcast_server_locked(self):
        self._broadcast_locked({"type": "server", "name": self.party_name, "icon": self._icon})

    # --- the relay server ------------------------------------------------------------------------------------

    def set_relay(self, actor_id, config):
        """The owner sets the group's relay server (relay/README.md), or removes it with None. Members' pages get new
        TURN credentials at once, and the app hands members new relay settings (relay_listeners)."""
        with self._lock:
            actor = self._members.get(actor_id)
            if actor is None or actor["role"] != "host":
                raise Forbidden("only the owner can set the relay server")
        try:
            cleaned = clean_relay(config) if config is not None else None
        except ValueError as error:
            raise Forbidden(str(error)) from None
        with self._lock:
            self._relay = cleaned
            self._save_locked()
            self._broadcast_ice_locked()
            listeners = list(self.relay_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logging.getLogger("privatepartynet.server").exception("A relay settings listener failed")
        return True

    def relay_view(self):
        """The relay settings for the owner's control panel, without the secret."""
        with self._lock:
            relay = self._relay
        if relay is None:
            return None
        return {"host": relay["host"], "port": relay["port"], "tls_port": relay["tls_port"], "pin": relay["pin"],
                "turn": relay["turn"]}

    def path_relay_for(self, user_id, sign_public=None, now=None):
        """What member `user_id`'s paths may use (pathway.PathManager.set_relay_config), signed by the owner:
        {"relay": {"vps": [...], "friend": {"owner", "pass"}, "expires"}, "sig"}. The VPS credential is the member's
        own (it holds a key, so it must travel only over the party's TLS connection). The friend-relay pass needs the
        member's identity key `sign_public` (hex) and is made only then. Both last TOKEN_SECONDS; hand out new settings
        well before "expires"."""
        now = time.time() if now is None else now
        with self._lock:
            relay = self._relay
        config = {"vps": [], "expires": int(now + relayproto.TOKEN_SECONDS)}
        if relay is not None:
            credential = relayproto.member_credential(relay["secret"], user_id, now=now).hex()
            for host in relay_hosts(relay):
                config["vps"].append({"host": host, "port": relay["port"], "tls_port": relay["tls_port"],
                                      "pin": relay["pin"], "credential": credential})
        if self.owner is None:
            return {"relay": config, "sig": ""}
        from privatepartynet.pathway import make_pass, sign_relay_config

        config["friend"] = {"owner": self.owner.sign_public.hex(), "pass": ""}
        if sign_public:
            config["friend"]["pass"] = make_pass(self.owner.sign, user_id, sign_public, now=now).hex()
        return sign_relay_config(config, self.owner.sign)

    def _relay_user(self):
        """The 16-byte id the host's own relay credentials carry: the owner's user id, or one made from the server's."""
        try:
            raw = bytes.fromhex(self._host_user_id or "")
        except ValueError:
            raw = b""
        if len(raw) == 16:
            return raw
        return hashlib.sha256(b"PrivatePartyNet host|" + self.server_id.encode("ascii", "replace")).digest()[:16]

    def invite_relays(self, secret, now=None):
        """The relays an invite carries, so its holder can reach the party through one when no direct way works
        (punch.py): the group's relay server and up to three members who relay for others, each with credentials for
        the invite's own id. They last as long as the invite, and whoever holds the invite holds them."""
        now = time.time() if now is None else now
        lifetime = self.INVITE_DAYS * 86400
        with self._lock:
            relay = self._relay
        found = []
        from privatepartynet.pathway import invite_key, make_pass

        _, public, joiner = invite_key(secret)
        if relay is not None:
            credential = relayproto.member_credential(relay["secret"], joiner, lifetime, now).hex()
            for host in relay_hosts(relay):
                found.append(["vps", host, relay["port"], relay["tls_port"], relay["pin"], credential])
        if self.owner is not None:
            friend_pass = make_pass(self.owner.sign, joiner, public, lifetime, now).hex()
            for address, port in list(self.friend_relays())[:3]:
                found.append(["friend", address, port, 0, "", friend_pass])
        return found

    def host_relays(self, now=None):
        """The relays the host keeps the invites' channels bound on, with the host's own credentials: the same ones
        invite_relays hands out, so a joiner and the host meet on each."""
        now = time.time() if now is None else now
        me = self._relay_user()
        with self._lock:
            relay = self._relay
        found = []
        if relay is not None:
            credential = relayproto.HmacCredential(relayproto.member_credential(relay["secret"], me, now=now))
            for host in relay_hosts(relay):
                found.append({"host": host, "port": relay["port"], "tls_port": relay["tls_port"], "pin": relay["pin"],
                              "credential": credential})
        if self.owner is not None:
            from privatepartynet.pathway import PassCredential, make_pass

            credential = PassCredential(make_pass(self.owner.sign, me.hex(), self.owner.sign_public, now=now),
                                        self.owner.sign)
            for address, port in list(self.friend_relays())[:3]:
                found.append({"host": address, "port": port, "tls_port": 0, "pin": "", "credential": credential})
        return found

    def _turn_locked(self, member):
        """TURN servers with credentials for one member's page (coturn beside the relay server), or []."""
        relay = self._relay
        if relay is None or not relay.get("turn"):
            return []
        username, credential = relayproto.turn_credentials(relay["secret"], member.get("user") or member["id"],
                                                           TURN_SECONDS)
        urls = []
        for host in relay_hosts(relay):
            host = f"[{host}]" if ":" in host else host
            urls += [f"turn:{host}:{TURN_PORT}?transport=udp", f"turn:{host}:{TURN_PORT}?transport=tcp"]
        return [{"urls": urls, "username": username, "credential": credential}]

    def _broadcast_ice_locked(self):
        """Tell every open page how its connections may cross routers: STUN, and TURN with its own credentials."""
        for member in self._members.values():
            if member["queue"] is not None:
                member["queue"].put({"type": "ice", "stun": self.remote, "turn": self._turn_locked(member)})

    # --- the game network ------------------------------------------------------------------------------------

    def net_directory(self):
        """This server's game network directory, made the first time it is needed (its network is chosen then and kept)."""
        with self._lock:
            if self._net is None:
                if self._net_subnet is None:
                    self._net_subnet = str(gamenet.subnet_for(self.server_id, gamenet.local_networks()))
                self._net = gamenet.Directory(self._net_subnet, self._net_vips, on_change=lambda _: self._save_net(),
                                              host_user=self.net_host_user)
                self._save_locked()
            return self._net

    @property
    def net_host_user(self):
        return self._host_user_id or "host"

    def _save_net(self):
        with self._lock:
            self._save_locked()

    def net_info(self):
        """{network, host} of this server's game network, without starting it."""
        with self._lock:
            subnet = self._net_subnet
        if subnet is None:
            return None
        import ipaddress as _ip

        network = _ip.ip_network(subnet)
        return {"network": str(network), "host": str(network.network_address + 1)}

    def net_open(self, card, nonce, signature, key, candidates=()):
        """Check a member who asks for a game network link and put them on it. Returns (member id, user id, address,
        stop event).

        The member proves who they are as at the front door, signing their WireGuard key with it, and must be in the
        party now, online, and not a guest. A second link from the same member ends the first."""
        try:
            user, sign, dh = card["id"], card["sign"], card["dh"]
            if user_id_of(sign, dh) != user:
                raise ValueError("id")
        except (KeyError, TypeError, ValueError):
            raise Forbidden("Your identity could not be checked.") from None
        with self._lock:
            until = self._challenges.pop(str(nonce), None)
        if until is None or until < time.monotonic():
            raise Forbidden("The sign-in step expired. Try again.")
        if not isinstance(signature, str) or not verify(sign, signature, net_message(self.fingerprint, nonce, key)):
            raise Forbidden("Your identity could not be checked.")
        directory = self.net_directory()
        with self._lock:
            member = next((m for m in self._members.values() if m.get("user") == user and m["online"]
                           and m["role"] in ("host", "admin", "member")), None)
            if member is None:
                raise Forbidden("Join the server first. Guests are not on the game network.")
            self._end_net_locked(member["id"])
            address = directory.join(user, key, candidates)
            stop = threading.Event()
            self._net_links[member["id"]] = (stop, user, key)
            return member["id"], user, address, stop

    def net_host(self, key, candidates=()):
        """Put the host's own PC on the game network with its WireGuard key. Returns its address (.1). The host's app
        follows the list with net_wait and net_roster, as a member's link does."""
        return self.net_directory().join(self.net_host_user, key, candidates)

    def net_host_off(self, key):
        self.net_directory().leave(self.net_host_user, key)

    def net_candidates(self, user, candidates):
        """A member's (or the host's) ways to be reached changed; everyone is sent the new list."""
        self.net_directory().set_candidates(user, candidates)

    def net_closed(self, member_id, stop):
        """A member's link ended by itself (they closed it, or the network dropped it). A member still in the party
        stays on the list, with the same address and key, so the others' tunnels to them stay up through a dropout
        while their app reconnects; they come off it when they leave or are taken out of the party."""
        with self._lock:
            link = self._net_links.get(member_id)
            if link is None or link[0] is not stop:
                return
            member = self._members.get(member_id)
            if member is None or member.get("leaving") or not member["online"] or self.reconnect_grace <= 0 or \
                    member.get("meter") is None:
                self._end_net_locked(member_id)
                return
            self._net_links.pop(member_id)
            stop.set()
            self._net_graced[member_id] = (link[1], link[2])

    def net_wait(self, version, timeout):
        """Wait for the list to change from `version`; returns the version now."""
        return self.net_directory().wait(version, timeout)

    def net_roster(self, user, sign_public=None):
        """The signed list of who is on, for one member, with the relay settings the path layer gives that member (bound
        to their identity's signing key, `sign_public`). Those settings are that member's own: they are only ever sent
        on that member's own link."""
        directory = self.net_directory()
        with self._lock:
            version, signed = self._net_signed
            if version != directory.version or signed is None:
                version = directory.version
                body = gamenet.roster_body(self.server_id, directory.network, version, directory.peers())
                signed = gamenet.sign_roster(body, self._signer)
                self._net_signed = (version, signed)
        relay_for = getattr(self, "path_relay_for", None)
        return {**signed, "relay": relay_for(user, sign_public=sign_public) if callable(relay_for) else None}

    # --- channels --------------------------------------------------------------------------------------------

    def channels(self):
        with self._lock:
            return self._channels_public()

    def _channels_public(self):
        return [dict(c) for c in self._channels]

    def _channel_locked(self, channel_id):
        return next((c for c in self._channels if c["id"] == channel_id), None)

    def _first_text_locked(self):
        return next((c["id"] for c in self._channels if c["kind"] == "text"), None)

    def _staff_locked(self, member_id, refusal="only the owner and admins can change channels"):
        member = self._members.get(member_id)
        if member is None or member["role"] not in STAFF:
            raise Forbidden(refusal)
        return member

    def add_channel(self, actor_id, name, kind="text"):
        """Make a text or voice channel. Returns it."""
        name = clean_channel_name(name)
        if name is None or kind not in ("text", "voice"):
            raise NotFound("a channel needs a name and a kind")
        with self._lock:
            self._staff_locked(actor_id)
            if len(self._channels) >= MAX_CHANNELS:
                raise Full("this server has the most channels it can have")
            channel = {"id": secrets.token_hex(4), "name": name, "kind": kind}
            self._channels.append(channel)
            if kind == "text":
                self._log[channel["id"]] = deque(maxlen=HISTORY)
                self._seq[channel["id"]] = 0
            self._save_locked()
            self._broadcast_channels_locked()
            return dict(channel)

    def rename_channel(self, actor_id, channel_id, name):
        name = clean_channel_name(name)
        with self._lock:
            self._staff_locked(actor_id)
            channel = self._channel_locked(channel_id)
            if channel is None or name is None:
                raise NotFound("no such channel")
            channel["name"] = name
            self._save_locked()
            self._broadcast_channels_locked()
            return dict(channel)

    def remove_channel(self, actor_id, channel_id):
        """Take a channel away. Its saved messages stay in the store; the last text channel cannot be removed."""
        with self._lock:
            self._staff_locked(actor_id)
            channel = self._channel_locked(channel_id)
            if channel is None:
                raise NotFound("no such channel")
            if channel["kind"] == "text" and sum(1 for c in self._channels if c["kind"] == "text") == 1:
                raise Forbidden("a server keeps at least one text channel")
            self._channels.remove(channel)
            self._log.pop(channel_id, None)
            for member in self._members.values():
                if member.get("voice") == channel_id:
                    member["voice"] = None
                    member["shares"] = {}  # their screens belonged to that channel
            self._save_locked()
            self._broadcast_channels_locked()
            self._broadcast_roster_locked()

    def _broadcast_channels_locked(self):
        self._broadcast_locked({"type": "channels", "channels": self._channels_public()})

    # --- voice -----------------------------------------------------------------------------------------------

    def join_voice(self, member_id, channel_id):
        """Be in a voice channel (or, with None, in none). Guests may be in one to watch its screens and listen; their
        pages never open a microphone, and other pages do not play a guest's sound. Leaving a voice channel, or moving to
        another, ends the person's screens: a screen belongs to the channel it was started in (set_share)."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not on this server")
            if channel_id is None:
                member["voice"] = None
            else:
                channel = self._channel_locked(channel_id)
                if channel is None or channel["kind"] != "voice":
                    raise NotFound("no such voice channel")
                others = sum(1 for m in self._members.values() if m.get("voice") == channel_id and m["id"] != member_id)
                if others >= MAX_VOICE and member.get("voice") != channel_id:
                    raise Full("that voice channel is full")
                if member.get("voice") != channel_id:
                    member["shares"] = {}
                member["voice"] = channel_id
            if channel_id is None:
                member["shares"] = {}
            self._broadcast_roster_locked()

    # --- messages --------------------------------------------------------------------------------------------

    def _clean_image_locked(self, image):
        """The image fields a message may carry: a hash of a picture this server holds, and its shape for the layout."""
        if not isinstance(image, dict) or not self._has_image(str(image.get("hash"))):
            raise NotFound("that picture was not uploaded")
        width, height = image.get("w"), image.get("h")
        ok = lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16384
        return {"hash": image["hash"], "w": width if ok(width) else 0, "h": height if ok(height) else 0}

    def _scope(self, channel_id):
        return f"srv:{self.server_id}:{channel_id}"

    def _wire(self, record):
        """A stored message as members receive it."""
        body = record["body"]
        return {"type": "chat", "id": record["id"], "channel": body.get("channel"), "seq": record["seq"],
                "from": body.get("from"), "user": record["sender"], "name": body.get("name", ""),
                "text": body.get("text", ""), "ts": record["ts"], "at": time.strftime("%H:%M", time.localtime(record["ts"])),
                **({"image": body["image"]} if body.get("image") else {}),
                **({"file": body["file"]} if body.get("file") else {})}

    def _load_logs(self):
        """Fill each text channel's recent messages from the store, so a restarted server remembers its chat."""
        for channel in self._channels:
            if channel["kind"] != "text":
                continue
            scope = self._scope(channel["id"])
            log = deque(maxlen=HISTORY)
            if self.store is not None:
                for record in self.store.history(scope, limit=HISTORY):
                    log.append(self._wire(record))
                self._seq[channel["id"]] = self.store.last_seq(scope)
            else:
                self._seq[channel["id"]] = 0
            self._log[channel["id"]] = log

    def _save_locked(self):
        if self.store is None:
            return
        self.store.save_server(self.server_id, {
            "name": self.party_name, "icon": self._icon, "avatars": self._avatars,
            "channels": self._channels, "roles": self._roles, "banned": sorted(self._banned),
            "known": self._known, "created": self._created, "password": self.passcode,
            "net": self._net_subnet, "vips": self._net.assignments() if self._net else self._net_vips,
            "invites": self._invite_secrets, "relay": self._relay,
            "files": {"limit": self._file_limit, "cap": self._storage_cap}})

    def chat(self, member_id, text, channel=None, image=None, file=None):
        """Post a chat message in a text channel (the first one when none is named) and save it. Returns the message,
        or None when the member is gone. Raises Forbidden for a guest and NotFound for a channel that is not a text channel."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return None
            if member["role"] == "guest":
                raise Forbidden("guests can read the chat but not post")
            if image is not None:
                image = self._clean_image_locked(image)
            channel_id = channel or self._first_text_locked()
            target = self._channel_locked(channel_id)
            if target is None or target["kind"] != "text":
                raise NotFound("no such text channel")
            seq = self._seq.get(channel_id, 0) + 1
            self._seq[channel_id] = seq
            now = max(time.time(), self._last_ts + 1e-6)  # the clock ticks every 15 ms on Windows; keep the order strict
            self._last_ts = now
            message = {"type": "chat", "id": secrets.token_hex(6), "channel": channel_id, "seq": seq, "from": member_id,
                       "user": member.get("user") or member_id, "name": member["name"], "text": text,
                       "ts": now, "at": time.strftime("%H:%M", time.localtime(now))}
            if image is not None:
                message["image"] = image
            if file is not None:
                message["file"] = file
            self._log.setdefault(channel_id, deque(maxlen=HISTORY)).append(message)
            if self.store is not None:
                self.store.add_message(message["id"], self._scope(channel_id), message["user"],
                                       {"text": text, "name": member["name"], "from": member_id, "channel": channel_id,
                                        **({"image": image} if image is not None else {}),
                                        **({"file": file} if file is not None else {})},
                                       ts=now, seq=seq)
                if image is not None:
                    self.store.hold(self.server_id, "image", image["hash"], self.store.blob_size(image["hash"]) or 0, uses=1)
                if file is not None:
                    self.store.hold(self.server_id, "file", file["id"], file["size"], uses=1)
            self._broadcast_locked(message)
            return message

    def put_image(self, member_id, data):
        """Keep an uploaded image and return {"hash", "mime", "size"}. Raises Forbidden for a guest or a member who is
        uploading too fast, and BadImage for anything that is not a PNG, JPEG, GIF or WebP under MAX_IMAGE."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not on this server")
            if member["role"] == "guest":
                raise Forbidden("guests can read the chat but not post")
            now = time.monotonic()
            recent = [t for t in self._image_times.get(member_id, []) if now - t < 60]
            if len(recent) >= IMAGES_PER_MINUTE:
                raise Forbidden("that is a lot of pictures; wait a moment")
            self._image_times[member_id] = recent + [now]
            if data:
                self._room_for_locked(len(data))
        if not data or len(data) > MAX_IMAGE:
            raise BadImage(f"pictures can be up to {MAX_IMAGE_TEXT}")
        mime = sniff_image(data)
        if mime is None:
            raise BadImage("only PNG, JPEG, GIF and WebP pictures can be sent")
        digest = hashlib.sha256(data).hexdigest()
        if self.store is not None:
            self.store.put_blob(data)
            self.store.hold(self.server_id, "image", digest, len(data))
            self._broadcast_storage()
        else:
            self._blobs[digest] = data
            while len(self._blobs) > 50 or sum(len(b) for b in self._blobs.values()) > MAX_MEMORY_BLOBS:
                self._blobs.pop(next(iter(self._blobs)))
        return {"hash": digest, "mime": mime, "size": len(data)}

    def get_image(self, member_id, digest):
        """(mime, bytes) of a stored image for a member, or None when there is no such image."""
        with self._lock:
            if member_id not in self._members:
                raise NotFound("you are not on this server")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        data = self.store.get_blob(digest) if self.store is not None else self._blobs.get(digest)
        if data is None:
            return None
        return sniff_image(data) or "application/octet-stream", data

    def _has_image(self, digest):
        return digest in self._blobs if self.store is None else self.store.has_blob(digest)

    # --- files, storage and deleting -------------------------------------------------------------------------

    def storage_info(self):
        """What members see under the server name: bytes this server keeps, its cap and per-file limit (None for none),
        and the space left on the host's disk. `files` is False for a party with no store, which keeps no files."""
        used, free = 0, None
        if self.store is not None:
            used = self.store.held_bytes(self.server_id)
            try:
                free = shutil.disk_usage(self.store.folder).free
            except OSError:
                free = None
        return {"used": used, "cap": self._storage_cap, "limit": self._file_limit, "free": free,
                "files": self.store is not None}

    def _broadcast_storage(self):
        info = {"type": "storage", **self.storage_info()}
        with self._lock:
            self._broadcast_locked(info)

    def set_file_settings(self, member_id, limit, cap):
        """The owner and admins set this server's per-file limit and storage cap. Returns the storage info."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["role"] not in STAFF:
                raise Forbidden("only the owner and admins set the file limits")
            problem = check_file_settings(limit, cap)
            if problem:
                raise BadRequest(problem)
            self._file_limit, self._storage_cap = limit, cap
            self._save_locked()
        self._broadcast_storage()
        return self.storage_info()

    def largest(self, member_id, limit=10):
        """The biggest pictures and files this server keeps, for the owner and admins: [{"kind", "size", "name"?}]."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["role"] not in STAFF:
                raise Forbidden("only the owner and admins see what takes the space")
        if self.store is None:
            return []
        return [{"kind": kind, "ref": ref, "size": size} for kind, ref, size in self.store.largest_held(self.server_id, limit)]

    def _room_for_locked(self, size, disk=True):
        """Raise Full when a picture or file of `size` bytes would pass this server's file limit, its storage cap or (with
        `disk`) the space the host's disk has to keep free."""
        if self._file_limit is not None and size > self._file_limit:
            raise Full(f"files can be up to {human_size(self._file_limit)} on this server")
        if self.store is None:
            return
        used = self.store.held_bytes(self.server_id)
        if self._storage_cap is not None and used + size > self._storage_cap:
            raise Full(f"this server's storage is full ({human_size(used)} of {human_size(self._storage_cap)}); "
                       "the owner or an admin can delete files or raise the cap")
        if disk:
            try:
                free = shutil.disk_usage(self.store.folder).free
            except OSError:
                return
            if size + MIN_FREE_BYTES > free:
                raise Full("the host's disk is nearly full")

    def begin_file(self, member_id, size, channel_id=None):
        """Check that a member may send a file of `size` bytes to a text channel now, and return the store's writer for
        it. Raises Forbidden, NotFound or Full with the reason."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not on this server")
            if member["role"] == "guest":
                raise Forbidden("guests can read the chat but not post")
            if self.store is None:
                raise Full("this server keeps no files")
            target = self._channel_locked(channel_id or self._first_text_locked())
            if target is None or target["kind"] != "text":
                raise NotFound("no such text channel")
            now = time.monotonic()
            recent = [t for t in self._file_times.get(member_id, []) if now - t < 60]
            if len(recent) >= FILES_PER_MINUTE:
                raise Forbidden("that is a lot of files; wait a moment")
            self._file_times[member_id] = recent + [now]
            self._room_for_locked(size)
        return self.store.begin_file()

    def finish_file(self, member_id, writer, channel_id, name, mime):
        """Keep a file that has arrived in full and post it to the channel. Returns the message. The limits are checked
        again, together with counting the file, so two uploads at once cannot both slip under the cap."""
        file_id, size, _digest = writer.finish()
        with self._lock:
            try:
                self._room_for_locked(size, disk=False)
            except Full:
                self.store.delete_file(file_id)
                raise
            self.store.hold(self.server_id, "file", file_id, size)
        info = {"id": file_id, "name": clean_file_name(name), "size": size, "type": clean_mime(mime)}
        try:
            message = self.chat(member_id, "", channel_id, file=info)
        except (Forbidden, NotFound):
            message = None
        if message is None:  # the sender left while it was arriving
            self.store.release(self.server_id, "file", file_id)
            self.store.delete_file(file_id)
            raise NotFound("you are not on this server")
        self._broadcast_storage()
        return message

    def _find_message_locked(self, channel_id, message_id):
        if not isinstance(message_id, str) or not message_id:
            return None
        found = next((m for m in self._log.get(channel_id, ()) if m["id"] == message_id), None)
        if found is None and self.store is not None:
            record = self.store.message(message_id, self._scope(channel_id))
            found = self._wire(record) if record else None
        return found

    def file_of(self, member_id, channel_id, message_id):
        """The file of a message, for a member to save: {"id", "name", "size", "type"}. Raises NotFound."""
        with self._lock:
            if member_id not in self._members:
                raise NotFound("you are not on this server")
            channel = self._channel_locked(channel_id)
            if channel is None or channel["kind"] != "text":
                raise NotFound("no such text channel")
            found = self._find_message_locked(channel_id, message_id)
        info = found.get("file") if found else None
        size = self.store.file_size(info.get("id")) if info and self.store is not None else None
        if size is None:
            raise NotFound("that file is no longer kept")
        return dict(info, size=size)

    def delete_message(self, member_id, channel_id, message_id):
        """Delete a message; its sender, the owner and admins may. Its picture or file is deleted from this PC too once no
        other message uses it, which frees the space. Copies members saved on their own PCs stay theirs."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not on this server")
            channel = self._channel_locked(channel_id)
            if channel is None or channel["kind"] != "text":
                raise NotFound("no such text channel")
            found = self._find_message_locked(channel_id, message_id)
            if found is None:
                raise NotFound("no such message")
            mine = found.get("user") == (member.get("user") or member_id) or found.get("from") == member_id
            if not mine and member["role"] not in STAFF:
                raise Forbidden("only its sender, the owner and admins can delete a message")
            log = self._log.get(channel_id)
            if log is not None:
                self._log[channel_id] = deque((m for m in log if m["id"] != message_id), maxlen=HISTORY)
            if self.store is not None:
                self.store.delete_message(message_id, self._scope(channel_id))
                if found.get("file") and self.store.release(self.server_id, "file", found["file"].get("id")):
                    self.store.delete_file(found["file"].get("id"))
                if found.get("image") and self.store.release(self.server_id, "image", found["image"].get("hash")):
                    self.store.delete_blob(found["image"].get("hash"))
            self._broadcast_locked({"type": "deleted", "channel": channel_id, "id": message_id})
        self._broadcast_storage()
        return True

    def channel_history(self, member_id, channel_id, before=None, limit=PAGE):
        """One page of older messages for a member scrolling back: up to `limit` messages before the time `before`,
        oldest first. Raises NotFound for an unknown member or channel."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if member_id not in self._members:
                raise NotFound("you are not on this server")
            channel = self._channel_locked(channel_id)
            if channel is None or channel["kind"] != "text":
                raise NotFound("no such text channel")
            if self.store is not None:
                return [self._wire(r) for r in self.store.history(self._scope(channel_id), limit=limit, before_ts=before)]
            older = [m for m in self._log.get(channel_id, ()) if before is None or m["ts"] < before]
            return older[-limit:]

    def relay(self, sender_id, to_id, signal):
        """Deliver a connection-setup signal from one member to another. False when the target is not online."""
        with self._lock:
            target = self._members.get(to_id)
            if target is None or target["queue"] is None or to_id == sender_id or sender_id not in self._members:
                return False
            target["queue"].put({**signal, "type": "signal", "from": sender_id})
            return True

    def cookie_name(self, base):
        """This party's own name for one of its cookies (MEMBER_COOKIE or HOST_COOKIE)."""
        return f"{base}_{self._cookie_suffix}"

    def set_theme(self, theme):
        """Change the theme of this party and tell every open party page, so the change shows without a reload."""
        with self._lock:
            self.theme = theme
            self._broadcast_locked({"type": "theme", "theme": theme})

    def roster(self):
        with self._lock:
            return [self._public(m) for m in self._members.values()]

    def _public(self, member):
        return {"id": member["id"], "name": member["name"], "role": member["role"],
                "user": member.get("user"), "voice": member.get("voice"),
                "avatar": self._avatars.get(member.get("user") or member["id"]),
                "online": member["online"], "sharing": bool(member["shares"]),
                "shares": [{"slot": slot, **info} for slot, info in sorted(member["shares"].items())],
                "link": self._link_public(member)}

    def _link_public(self, member):
        meter = member.get("meter")
        if member["role"] == "host":  # on this PC
            return {"state": "ok", "rtt": 0, "loss": 0.0, "since": int(self._started)}
        if meter is None:  # an app before 3.3 (or a Nearby accept): no heartbeat, so nothing is known but the connection
            return {"state": "ok", "rtt": None, "loss": None, "since": int(self._started)}
        return meter.public()

    def _end_net_locked(self, member_id):
        """End a member's game network link and take them off the list, so every other member drops them at once."""
        link = self._net_links.pop(member_id, None)
        graced = self._net_graced.pop(member_id, None)
        if link is not None:
            link[0].set()
        for entry in (graced, link[1:] if link is not None else None):
            if entry is not None and self._net is not None:
                self._net.leave(*entry)  # with the key: only if it is still the one on the list

    def _remove_locked(self, member_id, notice):
        self._end_net_locked(member_id)
        member = self._members.pop(member_id)
        if member.get("resume"):
            self._resume.pop(member["resume"], None)  # a removed, kicked or banned member's resume token dies with them
        for token, owner in list(self._sessions.items()):
            if owner == member_id:
                del self._sessions[token]
        if member["queue"] is not None and notice is not None:
            member["queue"].put(notice)
            member["queue"].put(_STOP)
        self._broadcast_roster_locked()

    def _broadcast_locked(self, message):
        for member in self._members.values():
            if member["queue"] is not None:
                member["queue"].put(message)

    def _broadcast_roster_locked(self):
        roster = {"type": "roster", "host": self._host_id,
                  "members": [self._public(m) for m in self._members.values()]}
        self._broadcast_locked(roster)


def _since_of(text):
    """{channel id: seq} from "?since=ch1:12,ch2:40" (what a page saw last), or None."""
    found = {}
    for part in str(text or "").split(",")[:64]:
        channel, _, seq = part.partition(":")
        if channel and seq.isdigit() and len(channel) <= 64:
            found[channel] = int(seq)
    return found or None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, party, secure):
        self.party = party
        self.secure = secure  # True for the LAN server (TLS); False for the loopback server (plain HTTP)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        """At most MAX_CONNECTIONS requests at once. One more is closed at once instead of taking a thread, so a flood
        of slow connections (the party is reachable from the internet when the router opened its port) cannot use up
        the PC."""
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        """A request ended in an exception. The other end hanging up is noted and nothing more; anything else goes in
        the log with its traceback, and the control panel says so in plain words (never the exception itself)."""
        exc = sys.exc_info()[1]
        where = "party" if self.secure else "party window"
        if is_hangup(exc):
            _Handler.hangup_log.info("The %s connection from %s closed early (%s)", where, client_address[0], describe(exc))
            return
        _Handler.hangup_log.error("The %s server could not answer a request from %s", where, client_address[0],
                                  exc_info=True)
        self.party.last_error = ("Your server could not answer one request; it keeps running. The details "
                                 "are in the log (Options, Open data folder).")


class _Handler(HangupsLogged, BaseHTTPRequestHandler):
    server_version = "PrivatePartyNet"
    hangup_what = "party"
    timeout = 60  # a silent client gives up after this; also bounds the TLS handshake

    def log_message(self, format, *args):
        pass  # pythonw has no stderr; problems show in the host window instead

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            if self._identity():
                self._send(200, HTML, self._render("room.html", capture="display"))
            else:
                self._send(200, HTML, self._render("login.html"))
        elif path == "/join":
            self._join()
        elif path == "/cpu":
            self._cpu()
        elif path == "/host":
            if not self._from_host_pc():
                self._send(403, TEXT, b"The host page only opens on the PC that runs PrivatePartyNet.")
                return
            self._send_with_cookie(self.server.party.cookie_name(HOST_COOKIE), self.server.party.host_token, self._render("room.html", capture="frames"))
        elif path == "/frames":
            self._frames()
        elif path == "/monitors":
            self._monitors()
        elif path == "/events":
            self._events()
        elif path == "/link":
            self._link_status()
        elif path == "/history":
            self._history()
        elif path == "/ask":
            self._ask_status()
        elif path == "/blob":
            self._blob_get()
        elif path == "/file":
            self._file_get()
        elif path == "/storage":
            self._storage_get()
        elif path.startswith("/static/") and path[len("/static/"):] in STATIC_TYPES:
            name = path[len("/static/"):]
            self._send(200, STATIC_TYPES[name], (WEB_DIR / name).read_bytes())
        else:
            self._send(404, TEXT, b"Not found.")

    def do_POST(self):
        path = urlsplit(self.path).path
        routes = {"/leave": self._leave, "/chat": self._chat, "/share": self._share,
                  "/signal": self._signal, "/name": self._name, "/kick": self._kick,
                  "/channel": self._channel, "/role": self._role, "/ban": self._ban, "/voice": self._voice,
                  "/decide": self._decide, "/ask": self._ask, "/blob": self._blob_put, "/net": self._net_link,
                  "/file": self._file_put, "/delete": self._delete, "/storage": self._storage_set,
                  "/server": self._server_set, "/server/icon": self._icon_put, "/avatar": self._avatar_put,
                  "/pong": self._pong, "/bye": self._bye, "/resume": self._resume}
        refused = self._post_refused(path)
        if refused:
            self._send(403, TEXT, refused.encode("utf-8"))
            return
        if path == "/log":
            self._page_log()
            return
        handler = routes.get(path)
        if handler is None:
            self._send(404, TEXT, b"Not found.")
            return
        handler()

    # --- handlers ---------------------------------------------------------------------------------------------

    def _cpu(self):
        """This PC's CPU use by the app, for the readout on the party page. Answers only on this PC."""
        if not self._from_host_pc():
            self._send(403, TEXT, b"The CPU reading is only for this PC.")
            return
        self._send(200, JSON_TYPE, json.dumps(cpu_detail()).encode("utf-8"))

    def _join(self):
        """A join link: swap the one-time token for a session, then continue to the party page."""
        query = parse_qs(urlsplit(self.path).query)
        session = self.server.party.redeem(query.get("token", [""])[0])
        if session is None:
            self._send(403, TEXT, b"This join link has expired or was already used. Ask the host to let you in again.")
            return
        self.server.party.note_join_address(self.client_address[0])
        rest = {key: values for key, values in query.items() if key != "token"}
        self._redirect("/" + (("?" + urlencode(rest, doseq=True)) if rest else ""),
                       self.server.party.cookie_name(MEMBER_COOKIE), session)

    def _pong(self):
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict):
            self._send(401, TEXT, b"Sign in first.")
            return
        link = self.server.party.pong(member, body.get("id"), self.client_address[0])
        self._send(200, JSON_TYPE, json.dumps(link or {}).encode("utf-8"))

    def _bye(self):
        member = self._identity()
        if member is not None:
            self.server.party.bye(member)
        self._send(202, TEXT, b"")

    def _link_status(self):
        """GET /link: where this page's member stands after a dropout. 200 with their link; 409 {"resume": true} when
        their app must resume first; 401 when they are no longer in the party."""
        member = self._identity()
        status = self.server.party.link_of(member) if member is not None else None
        if status is None:
            self._send(401, JSON_TYPE, json.dumps({"gone": True}).encode("utf-8"))
        elif status.get("resume"):
            self._send(409, JSON_TYPE, json.dumps(status).encode("utf-8"))
        else:
            self._send(200, JSON_TYPE, json.dumps(status).encode("utf-8"))

    def _resume(self):
        """POST /resume {"token", "card", "nonce", "sig"}: a member's app brings them back after a dropout."""
        body = self._json()
        if not isinstance(body, dict) or not isinstance(body.get("card"), dict):
            self._send(400, TEXT, b"Coming back needs your identity.")
            return
        try:
            name = self.server.party.resume(body.get("token"), body["card"], body.get("nonce"), body.get("sig"))
        except Forbidden as error:
            self._send(403, TEXT, str(error).encode("utf-8"))
            return
        self._send(200, JSON_TYPE, json.dumps({"ok": True, "name": name}).encode("utf-8"))

    def _leave(self):
        member = self._identity()
        if member is None or not self.server.party.leave(member):
            self._send(403, TEXT, b"You cannot leave this way.")
            return
        self._send(202, TEXT, b"")

    def _chat(self):
        member = self._identity()
        body = self._json()
        text = clean_chat(body.get("text") if isinstance(body, dict) else None)
        image = body.get("image") if isinstance(body, dict) else None
        if member is None or (text is None and image is None):
            self._send(400, TEXT, b"Chat needs a member and some text or a picture.")
            return
        channel = body.get("channel") if isinstance(body.get("channel"), str) else None
        if not self._attempt(lambda: self.server.party.chat(member, text or "", channel, image) is not None, 409,
                             b"You are not in the party."):
            return
        self._send(202, TEXT, b"")

    def _attempt(self, action, failure_status=409, failure_text=b"That did not work."):
        """Run a party action and answer for it. Forbidden is 403 and NotFound is 404, Full is 409. A False result is
        `failure_status`. Returns True when the action went through (the caller then sends its own success)."""
        self._refusal = ""
        try:
            result = action()
        except Forbidden as error:
            self._refusal = str(error)
            self._send(403, TEXT, str(error).encode("utf-8"))
            return False
        except NotFound as error:
            self._refusal = str(error)
            self._send(404, TEXT, str(error).encode("utf-8"))
            return False
        except Full as error:
            self._refusal = str(error)
            self._send(409, TEXT, str(error).encode("utf-8"))
            return False
        except BadRequest as error:
            self._send(400, TEXT, str(error).encode("utf-8"))
            return False
        if result is False:
            self._refusal = failure_text.decode("utf-8", "replace")
            self._send(failure_status, TEXT, failure_text)
            return False
        self._result = result
        return True

    def _history(self):
        """One older page of a text channel: /history?channel=ID&before=TIME. Members only."""
        member = self._identity()
        query = parse_qs(urlsplit(self.path).query)
        if member is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        try:
            before = float(query["before"][0]) if "before" in query else None
            limit = int(query.get("limit", [PAGE])[0])
        except ValueError:
            self._send(400, TEXT, b"before and limit are numbers.")
            return
        channel = query.get("channel", [""])[0]
        if not self._attempt(lambda: self.server.party.channel_history(member, channel, before, limit)):
            return
        self._send(200, JSON_TYPE, json.dumps({"channel": channel, "messages": self._result}).encode("utf-8"))

    def _channel(self):
        """{"action": "add"|"rename"|"remove", ...}: the owner and admins manage channels."""
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict):
            self._send(400, TEXT, b"Channels need a member and a request.")
            return
        party, action = self.server.party, body.get("action")
        if action == "add":
            done = self._attempt(lambda: party.add_channel(member, body.get("name"), body.get("kind", "text")))
        elif action == "rename":
            done = self._attempt(lambda: party.rename_channel(member, body.get("id"), body.get("name")))
        elif action == "remove":
            done = self._attempt(lambda: party.remove_channel(member, body.get("id")) or True)
        else:
            self._send(400, TEXT, b"Unknown channel action.")
            return
        if done:
            self._send(200, JSON_TYPE, json.dumps(self._result if isinstance(self._result, dict) else {}).encode("utf-8"))

    def _role(self):
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict) or not isinstance(body.get("id"), str) or not isinstance(body.get("role"), str):
            self._send(400, TEXT, b"A role change needs a member id and a role.")
            return
        if self._attempt(lambda: self.server.party.set_role(member, body["id"], body["role"])):
            self._send(202, TEXT, b"")

    def _ban(self):
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict):
            self._send(400, TEXT, b"Banning needs a member id.")
            return
        party = self.server.party
        if isinstance(body.get("unban"), str):
            ok = party.unban(member, body["unban"])
        elif isinstance(body.get("id"), str):
            ok = party.ban(member, body["id"])
        else:
            self._send(400, TEXT, b"Banning needs a member id.")
            return
        self._send(202 if ok else 403, TEXT, b"" if ok else b"Only the owner and admins can do that, to someone they may remove.")

    def _voice(self):
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict):
            self._send(400, TEXT, b"Voice needs a member and a channel.")
            return
        channel = body.get("channel")
        if channel is not None and not isinstance(channel, str):
            self._send(400, TEXT, b"Voice needs a channel id or null.")
            return
        if self._attempt(lambda: self.server.party.join_voice(member, channel) or True):
            self._send(202, TEXT, b"")

    def _blob_put(self):
        """An image upload: the raw bytes of one picture. Members only. The answer names it by hash."""
        member = self._identity()
        length = self._length()
        if member is None:
            self._refuse_body(length, 401, b"Sign in first.")
            return
        if length > MAX_IMAGE or length <= 0:
            self._refuse_body(length, 413, f"Pictures can be up to {MAX_IMAGE_TEXT}.".encode())
            return
        data = self.rfile.read(length)
        try:
            info = self.server.party.put_image(member, data)
        except BadImage as error:
            self._send(415 if "only" in str(error) else 413, TEXT, str(error).encode("utf-8"))
            return
        except Full as error:
            self._send(413, TEXT, str(error).encode("utf-8"))
            return
        except Forbidden as error:
            self._send(403, TEXT, str(error).encode("utf-8"))
            return
        except NotFound as error:
            self._send(404, TEXT, str(error).encode("utf-8"))
            return
        self._send(200, JSON_TYPE, json.dumps(info).encode("utf-8"))

    def _blob_get(self):
        member = self._identity()
        if member is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        digest = parse_qs(urlsplit(self.path).query).get("hash", [""])[0]
        try:
            found = self.server.party.get_image(member, digest)
        except NotFound:
            self._send(401, TEXT, b"Sign in first.")
            return
        if found is None:
            self._send(404, TEXT, b"No such picture.")
            return
        mime, data = found
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")  # the name is the hash of the bytes
        self.end_headers()
        self.wfile.write(data)

    def _file_put(self):
        """A file of any kind: POST /file?channel=ID&name=NAME&type=TYPE with its raw bytes. Members only. It is read and
        sealed a piece at a time as it arrives, kept, and posted to the channel; the answer is that message."""
        member = self._identity()
        length = self._length()
        query = parse_qs(urlsplit(self.path).query)
        if member is None:
            self._refuse_body(length, 401, b"Sign in first.")
            return
        if length <= 0:
            self._refuse_body(length, 411, b"A file needs its length.")
            return
        channel = query.get("channel", [""])[0] or None
        party = self.server.party
        try:
            writer = party.begin_file(member, length, channel)
        except (Forbidden, NotFound, Full) as error:
            status = 403 if isinstance(error, Forbidden) else 404 if isinstance(error, NotFound) else 413
            self._refuse_body(length, status, str(error).encode("utf-8"))
            return
        remaining = length
        try:
            while remaining:
                piece = self.rfile.read(min(UPLOAD_PIECE, remaining))
                if not piece:
                    raise ConnectionError("the upload stopped")
                writer.write(piece)
                remaining -= len(piece)
        except BaseException:
            writer.abort()  # a broken upload leaves nothing behind
            self.close_connection = True
            raise
        try:
            message = party.finish_file(member, writer, channel, query.get("name", [""])[0], query.get("type", [""])[0])
        except (Forbidden, NotFound, Full) as error:
            status = 403 if isinstance(error, Forbidden) else 404 if isinstance(error, NotFound) else 413
            self._send(status, TEXT, str(error).encode("utf-8"))
            return
        self._send(200, JSON_TYPE, json.dumps(message).encode("utf-8"))

    def _file_get(self):
        """GET /file?channel=ID&message=ID: the file of a message, to save. It always comes as a download of plain bytes,
        never as a page, so nothing a member sends can run here. A Range asks for part of it (a download carried on)."""
        member = self._identity()
        if member is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        query = parse_qs(urlsplit(self.path).query)
        party = self.server.party
        try:
            info = party.file_of(member, query.get("channel", [""])[0], query.get("message", [""])[0])
        except NotFound as error:
            self._send(404, TEXT, str(error).encode("utf-8"))
            return
        size = info["size"]
        start, end, status = 0, size, 200
        if self.headers.get("Range"):
            wanted = parse_range(self.headers.get("Range"), size)
            if wanted is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            (start, end), status = wanted, 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", attachment_header(info["name"]))
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cache-Control", "private, no-cache")
        self.end_headers()
        try:
            for piece in party.store.read_file(info["id"], start, end):
                self.wfile.write(piece)
        except VaultError:
            logging.getLogger("privatepartynet").warning("A kept file failed its check and was not sent")
            self.close_connection = True
        except (KeyError, OSError):
            self.close_connection = True

    def _delete(self):
        """{"channel", "id"}: delete a message. Its sender, the owner and admins may."""
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict):
            self._send(400, TEXT, b"Deleting needs a member, a channel and a message.")
            return
        if self._attempt(lambda: self.server.party.delete_message(member, body.get("channel"), body.get("id"))):
            self._send(202, TEXT, b"")

    def _storage_get(self):
        """GET /storage: what this server keeps and its limits; for the owner and admins also the biggest things kept."""
        member = self._identity()
        if member is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        party = self.server.party
        info = party.storage_info()
        try:
            info["largest"] = party.largest(member)
        except Forbidden:
            pass
        self._send(200, JSON_TYPE, json.dumps(info).encode("utf-8"))

    def _storage_set(self):
        """{"limit": bytes or null, "cap": bytes or null}: the owner and admins set this server's file limits."""
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict) or "limit" not in body or "cap" not in body:
            self._send(400, TEXT, b"File limits need a limit and a cap (each a size or none).")
            return
        if self._attempt(lambda: self.server.party.set_file_settings(member, body["limit"], body["cap"])):
            self._send(200, JSON_TYPE, json.dumps(self._result).encode("utf-8"))

    def _ask(self):
        """The front door: {"name", "password"?, "card"?, "nonce"?, "sig"?}. No login is needed, that is the point. A
        card with a signature over a challenge from GET /ask?challenge=1 proves who is asking; see Party.ask."""
        body = self._json()
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            self._send(400, TEXT, b"Asking to join needs a name.")
            return
        card = body.get("card") if isinstance(body.get("card"), dict) else None
        source = self.client_address[0]
        try:
            way = ("through the routers or a relay (a tunnel)" if ipaddress.ip_address(source) in TUNNEL_SOURCES
                   else f"from {source}")
        except ValueError:
            way = f"from {source}"
        if self._attempt(lambda: self.server.party.ask(body["name"], body.get("password") or "", source,
                                                       card, body.get("nonce"), body.get("sig"), body.get("caps"))):
            self._send(200, JSON_TYPE, json.dumps({"ticket": self._result}).encode("utf-8"))
            logging.getLogger("privatepartynet.server").info("A request to join %s (%s): taken; the owner or an admin "
                                                             "is asked, or a returning member comes in", way,
                                                             clean_name(body["name"]))
        else:
            logging.getLogger("privatepartynet.server").info("A request to join %s (%s): refused (%s)", way,
                                                             clean_name(body["name"]), self._refusal)

    def _net_link(self):
        """A member's game network link: {"card", "nonce", "sig", "key", "candidates"}, then the connection stays open.
        The host sends a JSON line each time the list of who is on changes (and an empty one as a keep-alive); the
        member sends a line when their ways to be reached change. It ends when either side closes it or the member
        leaves, and the member is then off the list."""
        party = self.server.party
        body = self._json()
        if not isinstance(body, dict) or not isinstance(body.get("card"), dict):
            self._send(400, TEXT, b"The game network needs your identity.")
            return
        try:
            member_id, user, address, stop = party.net_open(body["card"], body.get("nonce"), body.get("sig"),
                                                            body.get("key"), gamenet.clean_candidates(body.get("candidates")))
        except Forbidden as error:
            self._send(403, TEXT, str(error).encode("utf-8"))
            return
        except gamenet.NetError as error:
            self._send(409, TEXT, str(error).encode("utf-8"))
            return
        directory = party.net_directory()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        hello = {"address": str(address), "network": str(directory.network), "host": str(directory.host_address)}
        self.wfile.write(json.dumps(hello).encode("utf-8") + b"\n")
        self.wfile.flush()
        self.close_connection = True
        both = threading.Event()

        def watch():
            while not both.is_set():
                if stop.wait(0.5) or party.closing:
                    both.set()
            # Wake the reader at once: closing the handler's read side while a read is blocked would wait for that read
            # to time out (a minute), and the member would not notice the link had ended.
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def read():
            try:
                while not both.is_set():
                    line = self.rfile.readline(gamenet.MAX_CANDIDATES_BYTES + 64)
                    if not line.endswith(b"\n"):
                        break  # closed, or a line too long to be candidates
                    message = json.loads(line) if line.strip() else {}
                    if isinstance(message, dict) and "candidates" in message:
                        party.net_candidates(user, message["candidates"])
            except (OSError, ValueError):
                pass
            finally:
                both.set()
                party.net_closed(member_id, stop)  # off the list now, which also wakes the writer below

        threads =[threading.Thread(target=watch, daemon=True), threading.Thread(target=read, daemon=True)]
        for thread in threads:
            thread.start()
        version, sent = None, 0.0
        sign_public = body["card"]["sign"]
        try:
            while not both.is_set():
                due = gamenet.RELAY_REFRESH_SECONDS - (time.monotonic() - sent)
                current = party.net_wait(version, max(0.0, min(gamenet.KEEPALIVE_SECONDS, due)))
                if both.is_set():
                    break
                # The list again when it changed, and every RELAY_REFRESH_SECONDS anyway: the relay settings in it are
                # this member's own and run out after a few hours.
                fresh = current != version or time.monotonic() - sent >= gamenet.RELAY_REFRESH_SECONDS
                line = party.net_roster(user, sign_public) if fresh else {}
                if fresh:
                    sent = time.monotonic()
                version = current
                self.wfile.write(json.dumps(line).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            both.set()
            party.net_closed(member_id, stop)
            for thread in threads:
                thread.join(timeout=2)

    def _ask_status(self):
        query = parse_qs(urlsplit(self.path).query)
        if query.get("challenge") == ["1"]:
            party = self.server.party
            self._send(200, JSON_TYPE, json.dumps({"nonce": party.challenge(), "server": party.server_id,
                                                    "name": party.party_name}).encode("utf-8"))
            return
        ticket = query.get("ticket", [""])[0]
        self._send(200, JSON_TYPE, json.dumps(self.server.party.answer(ticket)).encode("utf-8"))

    def _decide(self):
        """The owner or an admin answers a join request: {"id": request, "allow": true|false}. The joiner picks up the
        answer themselves (GET /ask?ticket=); the join token never passes through the page of the person deciding."""
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict) or not isinstance(body.get("id"), str) or not isinstance(body.get("allow"), bool):
            self._send(400, TEXT, b"A decision needs a request id and allow true or false.")
            return
        if self._attempt(lambda: self.server.party.decide_request(member, body["id"], body["allow"]) or ""):
            self._send(200, JSON_TYPE, b"{}")

    def _share(self):
        member = self._identity()
        body = self._json()
        if member is None or not isinstance(body, dict) or not isinstance(body.get("sharing"), bool) or not _is_slot(body.get("slot")):
            self._send(400, TEXT, b"Sharing needs a member, a screen slot and a yes or no.")
            return
        if self._attempt(lambda: self.server.party.set_share(member, body["slot"], body["sharing"], body.get("label", ""),
                                                             body.get("width", 0), body.get("height", 0)),
                         400, b"Sharing needs a member, a screen slot and a yes or no."):
            self._send(202, TEXT, b"")

    def _server_set(self):
        """{"name": ...}: the owner or an admin renames the server."""
        member, body = self._identity(), self._json()
        if member is None or not isinstance(body, dict) or not isinstance(body.get("name"), str):
            self._send(400, TEXT, b"Renaming the server needs a name.")
            return
        if self._attempt(lambda: self.server.party.rename_server(member, body["name"])):
            self._send(200, JSON_TYPE, json.dumps({"name": self._result}).encode("utf-8"))

    def _picture_body(self):
        """The raw bytes of an icon or a person's picture, or None when the request was already answered. An empty body
        takes the picture away."""
        member = self._identity()
        length = self._length()
        if member is None:
            self._refuse_body(length, 401, b"Sign in first.")
            return None, None
        if length > MAX_PICTURE:
            self._refuse_body(length, 413, f"Pictures for icons can be up to {MAX_PICTURE_TEXT}.".encode())
            return None, None
        return member, (self.rfile.read(length) if length else b"")

    def _icon_put(self):
        member, data = self._picture_body()
        if member is None:
            return
        try:
            done = self._attempt(lambda: self.server.party.set_icon(member, data) or "")
        except BadImage as error:
            self._send(415, TEXT, str(error).encode("utf-8"))
            return
        if done:
            self._send(200, JSON_TYPE, json.dumps({"icon": self._result or None}).encode("utf-8"))

    def _avatar_put(self):
        member, data = self._picture_body()
        if member is None:
            return
        try:
            done = self._attempt(lambda: self.server.party.set_avatar(member, data) or "")
        except BadImage as error:
            self._send(415, TEXT, str(error).encode("utf-8"))
            return
        if done:
            self._send(200, JSON_TYPE, json.dumps({"avatar": self._result or None}).encode("utf-8"))

    def _signal(self):
        member = self._identity()
        body = self._json()
        signal = clean_signal(body)
        if member is None or signal is None:
            self._send(400, TEXT, b"Unexpected signal.")
            return
        if len(json.dumps(signal)) > MAX_BODY_BYTES:
            self._send(413, TEXT, b"Signal too large.")
            return
        if not self.server.party.relay(member, body["to"], signal):
            self._send(409, TEXT, b"That member is not online.")
            return
        self._send(202, TEXT, b"")

    def _name(self):
        member = self._identity()
        body = self._json()
        if member is None or not isinstance(body, dict) or not isinstance(body.get("name"), str):
            self._send(400, TEXT, b"Name needs a member and a name.")
            return
        self.server.party.rename(member, body["name"])
        self._send(202, TEXT, b"")

    def _kick(self):
        host = self._identity()
        body = self._json()
        if host is None or not isinstance(body, dict) or not isinstance(body.get("id"), str):
            self._send(400, TEXT, b"Kick needs a member id.")
            return
        if not self.server.party.kick(host, body["id"]):
            self._send(403, TEXT, b"Only the host can remove a member.")
            return
        self._send(202, TEXT, b"")

    def _monitors(self):
        party = self.server.party
        if not self._from_host_pc() or self._identity() != party.host_id:
            self._send(403, TEXT, b"The screen list is only for the host's own window.")
            return
        listed = party.grabber.monitors() if party.grabber is not None and hasattr(party.grabber, "monitors") else []
        self._send(200, JSON_TYPE, json.dumps(listed).encode("utf-8"))

    def _frames(self):
        party = self.server.party
        if not self._from_host_pc() or party.grabber is None or self._identity() != party.host_id:
            self._send(403, TEXT, b"Frames are only for the host's own window.")
            return
        query = parse_qs(urlsplit(self.path).query)
        fps = clamp_fps(query.get("fps", [None])[0])
        height = clamp_height(query.get("height", [None])[0])
        source = _pick_source(party.grabber, query.get("monitor", [None])[0])
        if source is None:
            self._send(400, TEXT, b"No such screen.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        frames = source.frames(fps, height, pointer=query.get("pointer", ["1"])[0] != "0")
        try:
            for jpeg in frames:
                if party.closing:
                    break
                self.wfile.write(struct.pack(">I", len(jpeg)) + jpeg)
                self.wfile.flush()
        except OSError as exc:
            # The host's page stopped reading: Go Live was turned off, or restarted the feed at another size. The
            # connection is finished too, or the next read on it would fail the same way (WinError 10053 in 3.1.0).
            self.close_connection = True
            self.hangup_log.info("The host window's frame feed ended (%s)", describe(exc) if is_hangup(exc) else exc)
        finally:
            frames.close()

    def _events(self):
        party = self.server.party
        member = self._identity()
        if member is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        if party.suspended(member):
            self._send(409, JSON_TYPE, b'{"resume": true}')
            return
        listener = party.attach(member, since=_since_of(parse_qs(urlsplit(self.path).query).get("since", [""])[0]))
        if listener is None:
            self._send(401, TEXT, b"Sign in first.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    message = listener.get(timeout=party.heartbeat)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")  # a failed write also tells us the browser has gone
                    continue
                if message is _STOP:
                    break
                self.wfile.write(b"data: " + json.dumps(message).encode("utf-8") + b"\n\n")
        except OSError as exc:
            self.close_connection = True  # the page has gone (closed, reloaded, or its network dropped)
            self.hangup_log.info("A party page's event stream from %s closed (%s)", self.client_address[0],
                                 describe(exc) if is_hangup(exc) else exc)
        finally:
            party.detach(member, listener)

    # --- helpers ----------------------------------------------------------------------------------------------

    def _cookies(self):
        try:
            return http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return http.cookies.SimpleCookie()

    def _page_log(self):
        """A page reports what it did or what went wrong. Lines go to the app log, so a fault in a window is visible."""
        body = self._json()
        message = body.get("message") if isinstance(body, dict) else None
        if isinstance(message, str):
            logging.getLogger("privatepartynet.page").warning("%s", message[:2000])
        self._send(202, TEXT, b"")

    def _identity(self):
        """The member id behind this request's cookies, or None. The member cookie wins over the host cookie."""
        party = self.server.party
        jar = self._cookies()
        for base in (MEMBER_COOKIE, HOST_COOKIE):
            morsel = jar.get(party.cookie_name(base))
            if morsel is not None:
                member = party.member_for(morsel.value)
                if member is not None:
                    return member
        return None

    def _from_host_pc(self):
        return self.client_address[0] == LOOPBACK

    def _refuse_body(self, length, status, text):
        """Answer a request whose body will not be used. The body is read first (up to MAX_DRAIN): a socket closed with
        unread data in it is reset, and the reset can reach the client before the answer does, so it never sees why.
        A longer body is not read at all; the connection closes after the answer instead."""
        if 0 < length <= MAX_DRAIN:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        elif length > MAX_DRAIN:
            self.close_connection = True
        self._send(status, TEXT, text)

    def _length(self):
        """The request's Content-Length, or 0 when it is missing, not a number, or negative (a negative length would
        make the read wait for the connection to close)."""
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _body(self, limit):
        length = min(self._length(), limit)
        return self.rfile.read(length) if length else b""

    def _json(self):
        try:
            return json.loads(self._body(MAX_BODY_BYTES) or b"null")
        except ValueError:
            return None

    def _render(self, name, error="", capture="display"):
        text = (WEB_DIR / name).read_text(encoding="utf-8")
        theme = self.server.party.theme
        text = text.replace("{{error}}", html.escape(error)).replace("{{capture}}", capture).replace("{{theme}}", theme)
        return text.encode("utf-8")

    def _post_refused(self, path):
        """Why a POST is refused, or "" to let it through. Every page and app client of the party sends its body declared
        as JSON (or as raw bytes, for a picture) and, from a page, from the party's own address. A page from anywhere else
        may send a plain form or text POST without asking first, and the party's cookie may ride along (rooms run inside
        the app's one window, next to other servers' rooms), so both are checked. Python clients send no Origin."""
        origin = self.headers.get("Origin")
        if origin is not None:
            host = self.headers.get("Host", "")
            own = ("https://" if self.server.secure else "http://") + host
            if not host or origin != own:
                return "That request came from another page."
        declared = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        wanted = "application/octet-stream" if path in ("/blob", "/file", "/server/icon", "/avatar") else "application/json"
        if declared != wanted:
            return "That request was not understood."
        return ""

    def _cookie_flags(self):
        # An HTTPS party's room runs in a frame of the app's window, which is another site (the app's own page on
        # 127.0.0.1), so its cookie has to be SameSite=None to be sent there. _post_refused keeps other pages out. The
        # host's own room is plain HTTP on 127.0.0.1, the same site as the app's page, and keeps Strict.
        return "HttpOnly; Secure; SameSite=None" if self.server.secure else "HttpOnly; SameSite=Strict"

    def _redirect(self, location, cookie=None, token=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", f"{cookie}={token}; Path=/; {self._cookie_flags()}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_with_cookie(self, cookie, token, body):
        self.send_response(200)
        self.send_header("Content-Type", HTML)
        self.send_header("Set-Cookie", f"{cookie}={token}; Path=/; {self._cookie_flags()}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
