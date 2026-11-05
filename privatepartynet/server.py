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
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from privatepartynet.cpu import sample_detail as cpu_detail
from privatepartynet.discovery import clean_name
from privatepartynet.frames import clamp_fps, clamp_height
from privatepartynet import gamenet
from privatepartynet.hangups import HangupsLogged, describe, is_hangup
from privatepartynet import relayproto
from privatepartynet.identity import user_id_of, verify
from privatepartynet.paths import web_dir

WEB_DIR = web_dir()
STATIC_TYPES = {
    "rtc.js": "application/javascript; charset=utf-8",
    "layout.js": "application/javascript; charset=utf-8",
    "netstats.js": "application/javascript; charset=utf-8",
    "pingview.js": "application/javascript; charset=utf-8",
    "capturesize.js": "application/javascript; charset=utf-8",
    "room.js": "application/javascript; charset=utf-8",
    "chat.js": "application/javascript; charset=utf-8",
    "voice.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "theme.css": "text/css; charset=utf-8",
}
MEMBER_COOKIE = "privatepartynet_member"
HOST_COOKIE = "privatepartynet_host"
VIEWER_COOKIE = "privatepartynet_viewer"  # a pop-out window's own session; it must never replace its parent's cookie
VIEWER_MARKER = "X-PrivatePartyNet-View"  # sent by pop-out pages, so a request says which of the two cookies is meant
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
MAX_VOICE = 6  # people in one voice channel: a full mesh of audio and video connections stays practical at this size
STAFF = ("host", "admin")  # the owner (internally "host") and the admins
ROLE_CHOICES = ("admin", "member", "guest")  # what the owner can give; "host" is the owner and "viewer" a pop-out window
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
MAX_DRAIN = 1024 * 1024  # what is read of a refused upload so the answer reaches the client; past it the connection closes
SIGNAL_KINDS = {"offer", "answer", "quality"}
TURN_PORT = 3478  # coturn beside the relay server (relay/turnserver.conf)
TURN_SECONDS = 24 * 3600  # how long the TURN credentials a page is given last; a page that reconnects gets new ones
GO_LIVE_QUALITIES = ("source", "1080", "720", "480")
GO_LIVE_FPS = (15, 30, 60)
MIN_SIZE, MAX_SIZE = 64, 16384
QUALITY_PERCENTS = (100, 75, 50, 25)
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
    """A voice channel already has MAX_VOICE people in it."""


class Party:
    """One party on this PC. Build it, call start(), and call stop() to end it.

    Never call stop() before start(), because shutdown() waits for the serving loop to run.
    """

    def __init__(self, passcode, port, certfile, keyfile, party_name, host_name,
                 failed_login_delay=1.0, heartbeat=15.0, grabber=None, theme="system",
                 store=None, server_id=None, host_user_id=None, trust_local=True, owner=None):
        self.trust_local = trust_local  # people on this network ask without the password (they still wait for a yes)
        # Addresses that look local but must not be trusted as such: the router, because some routers hand on
        # connections from the internet under their own address once a port is forwarded.
        self.untrusted = set()
        self.store = store  # the encrypted chat store; None keeps chat in memory for this run only
        self.server_id = server_id or secrets.token_hex(8)
        self._host_user_id = host_user_id
        self._signer = owner.sign if owner is not None else None  # the host identity's sign(data); it signs the game network's list of members
        self.theme = theme  # "system", "light" or "dark"; every page the party serves uses it
        self.grabber = grabber  # frames for the host's own screen; None disables the host's frame route
        self.party_name = clean_name(party_name)
        self.failed_login_delay = failed_login_delay
        self.heartbeat = heartbeat  # seconds between keep-alives; a failed keep-alive is how a dropped browser shows
        self.last_error = ""
        self._members = {}  # id -> record
        self._sessions = {}  # token -> member id
        self._joins = {}  # join token -> (name, expires at, role, parent, user id)
        record = (store.server(self.server_id) if store is not None else None) or {}
        self._channels = [dict(c) for c in record.get("channels") or default_channels()]
        self._roles = dict(record.get("roles") or {})  # user id -> "admin", "member" or "guest"
        self._banned = set(record.get("banned") or [])
        self._known = dict(record.get("known") or {})  # user id -> {"name", "role", "seen"}: everyone who has joined
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

    def join_token(self, name, role="member", parent=None, user=None):
        """A single-use join token for one person, valid for JOIN_SECONDS. The host's approval hands it to the
        joiner's app, which opens the party window with it. A browser cannot get one. A viewer token makes a
        pop-out window, which receives one screen and never shares its own, and closes with the member that opened it.
        `user` is the joiner's permanent id; with it the person keeps their role and cannot join while banned."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._joins[token] = (clean_name(name or "Guest"), time.monotonic() + JOIN_SECONDS, role, parent, user)
        return token

    def viewer_link(self, requester_id, target_id, slot):
        """A join path for a pop-out window that shows target's screen in slot, or None. See viewer_link_checked."""
        return self.viewer_link_checked(requester_id, target_id, slot)[0]

    def viewer_link_checked(self, requester_id, target_id, slot):
        """(path, reason). The path is None, with the reason, when the requester may not or the screen is not live.
        Only members make viewers; a viewer cannot make another one."""
        with self._lock:
            requester = self._members.get(requester_id)
            target = self._members.get(target_id)
            if requester is None:
                return None, "This window is not signed in to the party."
            if requester["role"] == "viewer":
                return None, "A pop-out window cannot open another one."
            if target is None:
                return None, "That person has left the party."
            if not _is_slot(slot) or slot not in target["shares"]:
                return None, "That screen is no longer being shared."
            name = f"{target['name']} window"
        token = self.join_token(name, role="viewer", parent=requester_id)
        return f"/join?token={token}&view={target_id}:{slot}", ""

    def redeem(self, token):
        """Turn a join token into a session token. None when the token is unknown, used or expired, or the party is full."""
        with self._lock:
            entry = self._joins.pop(token, None)
            if entry is None or time.monotonic() > entry[1]:
                return None
            name, _, role, parent, user = entry
            if user is not None and user in self._banned:
                return None
            if role != "viewer" and user is not None:
                role = self._roles.get(user, role)  # a returning person keeps the role they were given
            if user is not None:
                # a person who comes back replaces their own old, offline entry instead of adding a second one
                for old_id, old in list(self._members.items()):
                    if old.get("user") == user and old["role"] != "host" and not old["online"] and old_id != parent:
                        self._remove_locked(old_id, notice=None)
            if len(self._members) >= MAX_MEMBERS:
                return None
            member_id = secrets.token_hex(8)
            self._members[member_id] = {
                "id": member_id, "name": name, "role": role, "parent": parent, "user": user, "voice": None,
                "online": False, "shares": {}, "queue": None,
            }
            if user is not None and role != "viewer":
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
        """The role of the member behind a session token ("host", "member" or "viewer"), or None."""
        with self._lock:
            member = self._members.get(self._sessions.get(token))
            return member["role"] if member else None
    def member_for(self, token):
        """The member id behind a session token, or None."""
        with self._lock:
            return self._sessions.get(token)

    def attach(self, member_id):
        """Connect a member's event stream. Returns its queue, or None if the member has gone."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return None
            listener = queue.Queue()
            member["queue"] = listener
            member["online"] = True
            listener.put({"type": "hello", "you": self._public(member), "party": self.party_name, "host": self._host_id,
                          "theme": self.theme, "server": self.server_id, "channels": self._channels_public(),
                          "stun": self.remote, "turn": self._turn_locked(member)})
            for channel in self._channels:
                if channel["kind"] == "text":
                    listener.put({"type": "history", "channel": channel["id"],
                                  "messages": list(self._log.get(channel["id"], ()))})
            if member["role"] in STAFF:
                for request in self._requests_public_locked():
                    listener.put({"type": "join_request", **request})
            self._broadcast_roster_locked()
            return listener

    def detach(self, member_id, listener):
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["queue"] is not listener:
                return
            member["queue"] = None
            member["online"] = False
            member["shares"] = {}
            member["voice"] = None
            self._end_net_locked(member_id)  # closing the party window ends the game network link too
            if member["role"] == "viewer":
                self._remove_locked(member_id, notice=None)
                return
            self._broadcast_roster_locked()

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
        guests between those two. The owner's role and a pop-out window's role never change. Remembered by person."""
        with self._lock:
            actor, target = self._members.get(actor_id), self._members.get(target_id)
            if actor is None or target is None or role not in ROLE_CHOICES:
                raise NotFound("no such member or role")
            if target["role"] in ("host", "viewer") or target_id == actor_id:
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

    def request_join(self, name, user=None, address=""):
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
                                          "user": user, "address": address, "until": time.monotonic() + REQUEST_SECONDS}
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

    def ask(self, name, password, address="", card=None, nonce=None, signature=None):
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
            token = self.join_token(name, user=user)
            with self._lock:
                self._answers[ticket] = {"state": "allowed", "token": token, "until": time.monotonic() + JOIN_SECONDS}
            return ticket
        local = self.trust_local and is_local_address(address) and address not in self.untrusted
        given = str(password or "").encode("utf-8")
        if not local and not hmac.compare_digest(given, self.passcode.encode("utf-8")):
            self._failed(address)
            time.sleep(self.failed_login_delay)
            raise Forbidden("That password is not right.")
        return self.request_join(name, user, address)[1]

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
            token = self.join_token(request["name"], user=request["user"])
            self._answers[request["ticket"]] = {"state": "allowed", "token": token, "until": time.monotonic() + JOIN_SECONDS}
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
        """Start, update or stop one of a member's screens. Each member has SHARE_SLOTS slots, one per screen."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None or member["role"] in ("viewer", "guest") or not _is_slot(slot):
                return False
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
                raise Forbidden("Join the party first. Guests are not on the game network.")
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
        """A member's link ended by itself (they closed it, or the network dropped it): they are off the list."""
        with self._lock:
            link = self._net_links.get(member_id)
            if link is not None and link[0] is stop:
                self._end_net_locked(member_id)

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

    def _staff_locked(self, member_id):
        member = self._members.get(member_id)
        if member is None or member["role"] not in STAFF:
            raise Forbidden("only the owner and admins can change channels")
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
            self._save_locked()
            self._broadcast_channels_locked()
            self._broadcast_roster_locked()

    def _broadcast_channels_locked(self):
        self._broadcast_locked({"type": "channels", "channels": self._channels_public()})

    # --- voice -----------------------------------------------------------------------------------------------

    def join_voice(self, member_id, channel_id):
        """Be in a voice channel (or, with None, in none). Guests listen to the text and watch streams but do not talk."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not in the party")
            if channel_id is None:
                member["voice"] = None
            else:
                channel = self._channel_locked(channel_id)
                if channel is None or channel["kind"] != "voice":
                    raise NotFound("no such voice channel")
                if member["role"] in ("guest", "viewer"):
                    raise Forbidden("guests cannot join voice")
                others = sum(1 for m in self._members.values() if m.get("voice") == channel_id and m["id"] != member_id)
                if others >= MAX_VOICE:
                    raise Full("that voice channel is full")
                member["voice"] = channel_id
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
                **({"image": body["image"]} if body.get("image") else {})}

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
            "name": self.party_name, "channels": self._channels, "roles": self._roles, "banned": sorted(self._banned),
            "known": self._known, "created": self._created, "password": self.passcode,
            "net": self._net_subnet, "vips": self._net.assignments() if self._net else self._net_vips,
            "invites": self._invite_secrets, "relay": self._relay})

    def chat(self, member_id, text, channel=None, image=None):
        """Post a chat message in a text channel (the first one when none is named) and save it. Returns the message,
        or None when the member is gone. Raises Forbidden for a guest and NotFound for a channel that is not a text channel."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return None
            if member["role"] in ("guest", "viewer"):
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
            self._log.setdefault(channel_id, deque(maxlen=HISTORY)).append(message)
            if self.store is not None:
                self.store.add_message(message["id"], self._scope(channel_id), message["user"],
                                       {"text": text, "name": member["name"], "from": member_id, "channel": channel_id,
                                        **({"image": image} if image is not None else {})},
                                       ts=now, seq=seq)
            self._broadcast_locked(message)
            return message

    def put_image(self, member_id, data):
        """Keep an uploaded image and return {"hash", "mime", "size"}. Raises Forbidden for a guest or a member who is
        uploading too fast, and BadImage for anything that is not a PNG, JPEG, GIF or WebP under MAX_IMAGE."""
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                raise NotFound("you are not in the party")
            if member["role"] in ("guest", "viewer"):
                raise Forbidden("guests can read the chat but not post")
            now = time.monotonic()
            recent = [t for t in self._image_times.get(member_id, []) if now - t < 60]
            if len(recent) >= IMAGES_PER_MINUTE:
                raise Forbidden("that is a lot of pictures; wait a moment")
            self._image_times[member_id] = recent + [now]
        if not data or len(data) > MAX_IMAGE:
            raise BadImage(f"pictures can be up to {MAX_IMAGE_TEXT}")
        mime = sniff_image(data)
        if mime is None:
            raise BadImage("only PNG, JPEG, GIF and WebP pictures can be sent")
        digest = hashlib.sha256(data).hexdigest()
        if self.store is not None:
            self.store.put_blob(data)
        else:
            self._blobs[digest] = data
            while len(self._blobs) > 50 or sum(len(b) for b in self._blobs.values()) > MAX_MEMORY_BLOBS:
                self._blobs.pop(next(iter(self._blobs)))
        return {"hash": digest, "mime": mime, "size": len(data)}

    def get_image(self, member_id, digest):
        """(mime, bytes) of a stored image for a member, or None when there is no such image."""
        with self._lock:
            if member_id not in self._members:
                raise NotFound("you are not in the party")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        data = self.store.get_blob(digest) if self.store is not None else self._blobs.get(digest)
        if data is None:
            return None
        return sniff_image(data) or "application/octet-stream", data

    def _has_image(self, digest):
        return digest in self._blobs if self.store is None else self.store.has_blob(digest)

    def channel_history(self, member_id, channel_id, before=None, limit=PAGE):
        """One page of older messages for a member scrolling back: up to `limit` messages before the time `before`,
        oldest first. Raises NotFound for an unknown member or channel."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if member_id not in self._members:
                raise NotFound("you are not in the party")
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
                "online": member["online"], "sharing": bool(member["shares"]),
                "shares": [{"slot": slot, **info} for slot, info in sorted(member["shares"].items())]}

    def _end_net_locked(self, member_id):
        """End a member's game network link and take them off the list, so every other member drops them at once."""
        link = self._net_links.pop(member_id, None)
        if link is not None:
            stop, user, key = link
            stop.set()
            if self._net is not None:
                self._net.leave(user, key)

    def _remove_locked(self, member_id, notice):
        self._end_net_locked(member_id)
        member = self._members.pop(member_id)
        for child_id in [mid for mid, m in self._members.items() if m.get("parent") == member_id]:
            self._remove_locked(child_id, notice={"type": "left"})  # pop-out windows close with the member that opened them
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
        self.party.last_error = ("The party server could not answer one request; the party keeps running. The details "
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
            if self._identity() or self._identity(viewer=True):  # a pop-out page is the same page, under its own cookie
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
            self._send_with_cookie(HOST_COOKIE, self.server.party.host_token, self._render("room.html", capture="frames"))
        elif path == "/frames":
            self._frames()
        elif path == "/monitors":
            self._monitors()
        elif path == "/events":
            self._events()
        elif path == "/history":
            self._history()
        elif path == "/ask":
            self._ask_status()
        elif path == "/blob":
            self._blob_get()
        elif path.startswith("/static/") and path[len("/static/"):] in STATIC_TYPES:
            name = path[len("/static/"):]
            self._send(200, STATIC_TYPES[name], (WEB_DIR / name).read_bytes())
        else:
            self._send(404, TEXT, b"Not found.")

    def do_POST(self):
        path = urlsplit(self.path).path
        routes = {"/leave": self._leave, "/chat": self._chat, "/share": self._share,
                  "/signal": self._signal, "/name": self._name, "/kick": self._kick, "/popout": self._popout,
                  "/channel": self._channel, "/role": self._role, "/ban": self._ban, "/voice": self._voice,
                  "/decide": self._decide, "/ask": self._ask, "/blob": self._blob_put, "/net": self._net_link}
        if path == "/log":
            self._page_log()
            return
        handler = routes.get(path)
        if handler is None:
            self._send(404, TEXT, b"Not found.")
            return
        handler()

    # --- handlers ---------------------------------------------------------------------------------------------

    def _popout(self):
        """A member asks for a window that shows one screen of another member. The window joins as a viewer."""
        member = self._identity()
        body = self._json()
        path, reason = None, "That request was not understood."
        if isinstance(body, dict):
            path, reason = self.server.party.viewer_link_checked(member, body.get("target"), body.get("slot"))
        if path is None:
            logging.getLogger("privatepartynet.page").info("Pop-out refused: %s", reason)
            self._send(400, TEXT, reason.encode("utf-8"))
            return
        self._send(200, JSON_TYPE, json.dumps({"path": path}).encode("utf-8"))

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
        # A pop-out window shares the app's cookie jar with the window that opened it, so it gets a cookie of its own.
        cookie = VIEWER_COOKIE if self.server.party.role_for(session) == "viewer" else MEMBER_COOKIE
        self._redirect("/" + (("?" + urlencode(rest, doseq=True)) if rest else ""), cookie, session)

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
        except Forbidden as error:
            self._send(403, TEXT, str(error).encode("utf-8"))
            return
        except NotFound as error:
            self._send(404, TEXT, str(error).encode("utf-8"))
            return
        self._send(200, JSON_TYPE, json.dumps(info).encode("utf-8"))

    def _blob_get(self):
        member = self._identity() or self._identity(viewer=True)
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
                                                       card, body.get("nonce"), body.get("sig"))):
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
        if not self.server.party.set_share(member, body["slot"], body["sharing"], body.get("label", ""),
                                           body.get("width", 0), body.get("height", 0)):
            self._send(400, TEXT, b"Sharing needs a member, a screen slot and a yes or no.")
            return
        self._send(202, TEXT, b"")

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
        listener = party.attach(member)
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

    def _identity(self, viewer=None):
        """The member id behind this request's cookies, or None. A pop-out page says so (a header, or ?as=viewer on the
        event stream) and is known by its viewer cookie. Every other request is known by the member cookie, which wins
        over the host cookie, and never by a viewer cookie. Pass viewer to choose instead of reading the request."""
        party = self.server.party
        jar = self._cookies()
        from_viewer = viewer
        if from_viewer is None:
            from_viewer = self.headers.get(VIEWER_MARKER) == "1" or parse_qs(urlsplit(self.path).query).get("as") == ["viewer"]
        for name in ((VIEWER_COOKIE,) if from_viewer else (MEMBER_COOKIE, HOST_COOKIE)):
            morsel = jar.get(name)
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

    def _cookie_flags(self):
        return "HttpOnly; Secure; SameSite=Strict" if self.server.secure else "HttpOnly; SameSite=Strict"

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
