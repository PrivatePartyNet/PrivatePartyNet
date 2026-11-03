"""The relay: what passes between a PC and a relay, and the relay's forwarding rules. Standard library only, because the
relay server on a rented Linux machine (relay/ppn_relay.py) runs this same file without the rest of the app.

A relay forwards datagrams between two PCs that cannot reach each other. It is either a server the group runs itself (a
"VPS relay", set up from relay/README.md) or a party member whose PC others can reach (a "friend relay", see
pathway.py). Both speak this protocol and keep the same rules.

How two PCs meet on a relay: each sends BIND for the same 16-byte channel, which both derive from a secret only the two
of them hold, with a token that shows the relay they may use it. The relay then hands every SEND from one end to the
other as DELIVER. It never knows who the two are beyond the user id in the token, and what it forwards is WireGuard or
QUIC, already encrypted end to end: a relay cannot read or change the traffic, only drop it.

Tokens: expiry (4 bytes, Unix seconds) + user id (16 bytes) + the first 16 bytes of HMAC-SHA256(key, label + expiry +
user). The relay checks them with its key and needs no list of users. For a VPS relay the key is the secret the owner
typed in; the owner's PC mints tokens and hands them to members, so the secret itself never leaves it. For a friend
relay the key is the party's own relay key, which every member holds. TURN credentials for coturn (voice and screens)
come from the same secret in coturn's own form (use-auth-secret).

Messages (one per UDP datagram; on the TCP fallback each is preceded by its length in two bytes):

    BIND     0x20 channel(16) token(36)        a PC asks to be one end of a channel; sent again every 15 s to stay
    BOUND    0x21 channel(16) status(1)        the relay's answer; status 0 is yes
    SEND     0x22 channel(16) payload          a PC sends a datagram to the other end
    DELIVER  0x23 channel(16) payload          the relay hands it on

A relay keeps at most two ends per channel, one per user id; a second BIND from the same user (a new port after the
router changed it, or the TCP fallback) moves that end. Each end has its own rate limit, and a channel nobody has used
for IDLE_SECONDS is forgotten.
"""

import base64
import hashlib
import hmac
import os
import struct
import time

BIND, BOUND, SEND, DELIVER = 0x20, 0x21, 0x22, 0x23
OK, BAD_TOKEN, CHANNEL_FULL, RELAY_FULL = 0, 1, 2, 3
STATUS_TEXT = {OK: "ok", BAD_TOKEN: "the relay did not accept the credentials", CHANNEL_FULL: "the channel is in use",
               RELAY_FULL: "the relay is full"}
CHANNEL_BYTES = 16
TOKEN_BYTES = 36
HEADER = 1 + CHANNEL_BYTES  # what a SEND or DELIVER adds to a datagram
MAX_PAYLOAD = 1408  # the largest datagram a relay forwards: 1400 for the paths, plus their own one-byte marker, with room
TOKEN_LABEL = b"PrivatePartyNet relay|"
TOKEN_SECONDS = 24 * 3600
REBIND_SECONDS = 15.0  # how often a PC sends BIND again; it also keeps routers open
IDLE_SECONDS = 60.0
DEFAULT_RATE = 1_000_000  # bytes a second for one end of one channel
DEFAULT_BURST = 256 * 1024
MAX_CHANNELS = 4096
MAX_CHANNELS_PER_USER = 64
DEFAULT_UDP_PORT = 3479  # coturn keeps 3478
DEFAULT_TLS_PORT = 443


class RelayError(Exception):
    pass


# --- keys, tokens and channels ---------------------------------------------------------------------------------------


def secret_key(secret):
    """The HMAC key for a relay secret as typed (text) or as bytes."""
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def _user_bytes(user):
    raw = bytes.fromhex(user) if isinstance(user, str) else bytes(user)
    if len(raw) != 16:
        raise ValueError("a user id is 16 bytes")
    return raw


def mint_token(secret, user, lifetime=TOKEN_SECONDS, now=None):
    """A token for `user` (hex or 16 bytes) that a relay holding `secret` accepts until it expires."""
    expiry = struct.pack("!I", int((now if now is not None else time.time()) + lifetime))
    user = _user_bytes(user)
    return expiry + user + hmac.new(secret_key(secret), TOKEN_LABEL + expiry + user, hashlib.sha256).digest()[:16]


def check_token(secret, token, now=None):
    """The user id (hex) a token was made for, or None when it is malformed, forged or expired."""
    if not isinstance(token, (bytes, bytearray)) or len(token) != TOKEN_BYTES:
        return None
    expiry, user, mac = token[:4], token[4:20], token[20:]
    wanted = hmac.new(secret_key(secret), TOKEN_LABEL + expiry + user, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, wanted):
        return None
    if struct.unpack("!I", expiry)[0] < (now if now is not None else time.time()):
        return None
    return user.hex()


def token_expiry(token):
    return struct.unpack("!I", token[:4])[0] if len(token) == TOKEN_BYTES else 0


def turn_credentials(secret, user, lifetime=TOKEN_SECONDS, now=None):
    """(username, credential) for coturn's use-auth-secret ("TURN REST API"): the username is expiry:user and the
    credential base64(HMAC-SHA1(secret, username))."""
    username = f"{int((now if now is not None else time.time()) + lifetime)}:{user}"
    credential = base64.b64encode(hmac.new(secret_key(secret), username.encode("utf-8"), hashlib.sha1).digest())
    return username, credential.decode("ascii")


def channel_for(label, *secrets_):
    """A channel id from secrets only its two ends hold (a pair key, an invite secret). The label keeps uses apart."""
    digest = hashlib.sha256(b"PrivatePartyNet channel|" + label.encode("ascii"))
    for item in secrets_:
        digest.update(len(item).to_bytes(2, "big") + bytes(item))
    return digest.digest()[:CHANNEL_BYTES]


def new_secret():
    """A relay secret for an owner who has none yet: 32 random characters, safe in a config file or a command line."""
    return base64.b32encode(os.urandom(20)).decode("ascii").lower()


# --- messages --------------------------------------------------------------------------------------------------------


def bind(channel, token):
    return bytes([BIND]) + bytes(channel) + bytes(token)


def bound(channel, status):
    return bytes([BOUND]) + bytes(channel) + bytes([status])


def send(channel, payload):
    return bytes([SEND]) + bytes(channel) + bytes(payload)


def deliver(channel, payload):
    return bytes([DELIVER]) + bytes(channel) + bytes(payload)


def parse(data):
    """(type, channel, rest) of a relay message, or None when it is not one."""
    if len(data) < HEADER or data[0] not in (BIND, BOUND, SEND, DELIVER):
        return None
    kind, channel, rest = data[0], bytes(data[1:HEADER]), bytes(data[HEADER:])
    if kind == BIND and len(rest) != TOKEN_BYTES:
        return None
    if kind == BOUND and len(rest) != 1:
        return None
    return kind, channel, rest


def is_relay_message(data):
    return bool(data) and data[0] in (BIND, BOUND, SEND, DELIVER)


# --- the relay's rules -----------------------------------------------------------------------------------------------


class Bucket:
    """A token bucket over bytes: `rate` a second, at most `burst` at once."""

    __slots__ = ("rate", "burst", "level", "stamp")

    def __init__(self, rate, burst, now):
        self.rate, self.burst, self.level, self.stamp = rate, burst, burst, now

    def take(self, size, now):
        self.level = min(self.burst, self.level + (now - self.stamp) * self.rate)
        self.stamp = now
        if size > self.level:
            return False
        self.level -= size
        return True


class RelayCore:
    """The forwarding table of one relay, apart from any socket: `handle(data, end)` takes what arrived from `end` (any
    hashable value that names a way back, such as ("udp", address)) and returns what to send, as [(end, bytes)].

    `keys` are the HMAC keys whose tokens are accepted. `rate`/`burst` limit each end of each channel, and `total_rate`
    (None for no limit) everything together, which is how a friend relay keeps to the bandwidth its owner allows."""

    def __init__(self, keys, rate=DEFAULT_RATE, burst=DEFAULT_BURST, total_rate=None, max_channels=MAX_CHANNELS,
                 max_per_user=MAX_CHANNELS_PER_USER, idle=IDLE_SECONDS, clock=time.monotonic, wall=time.time):
        self.keys = [secret_key(k) for k in keys]
        self.rate, self.burst = rate, burst
        self.max_channels, self.max_per_user, self.idle = max_channels, max_per_user, idle
        self._clock, self._wall = clock, wall
        self.total = None
        self.set_total_rate(total_rate)
        self.channels = {}  # channel -> {user: {"end", "bucket", "seen"}}
        self.stats = {"forwarded": 0, "bytes": 0, "limited": 0, "refused": 0, "dropped": 0}
        self._swept = clock()

    def set_total_rate(self, total_rate):
        """Limit everything together to `total_rate` bytes a second (a quarter of a second's worth at once), or None."""
        self.total = Bucket(total_rate, max(total_rate // 4, 16 * MAX_PAYLOAD), self._clock()) if total_rate else None

    def set_keys(self, keys):
        self.keys = [secret_key(k) for k in keys]

    def _user(self, token):
        now = self._wall()
        for key in self.keys:
            user = check_token(key, token, now)
            if user is not None:
                return user
        return None

    def handle(self, data, end):
        now = self._clock()
        if now - self._swept > self.idle / 4:
            self.sweep(now)
        message = parse(data)
        if message is None:
            self.stats["dropped"] += 1
            return []
        kind, channel, rest = message
        if kind == BIND:
            return [(end, bound(channel, self._bind(channel, rest, end, now)))]
        if kind != SEND:
            self.stats["dropped"] += 1
            return []
        ends = self.channels.get(channel)
        sender = next((u for u, e in ends.items() if e["end"] == end), None) if ends else None
        if sender is None or len(rest) > MAX_PAYLOAD:
            self.stats["dropped"] += 1
            return []
        me = ends[sender]
        me["seen"] = now
        other = next((e for u, e in ends.items() if u != sender), None)
        if other is None:
            self.stats["dropped"] += 1
            return []
        size = len(rest) + HEADER
        if not me["bucket"].take(size, now) or (self.total is not None and not self.total.take(size, now)):
            self.stats["limited"] += 1
            return []
        self.stats["forwarded"] += 1
        self.stats["bytes"] += size
        return [(other["end"], deliver(channel, rest))]

    def _bind(self, channel, token, end, now):
        user = self._user(token)
        if user is None:
            self.stats["refused"] += 1
            return BAD_TOKEN
        ends = self.channels.get(channel)
        if ends is None:
            if len(self.channels) >= self.max_channels:
                self.stats["refused"] += 1
                return RELAY_FULL
            if sum(1 for e in self.channels.values() if user in e) >= self.max_per_user:
                self.stats["refused"] += 1
                return RELAY_FULL
            ends = self.channels[channel] = {}
        if user in ends:
            ends[user]["end"], ends[user]["seen"] = end, now  # the same person from a new port, or over TCP now
            return OK
        if len(ends) >= 2:
            self.stats["refused"] += 1
            return CHANNEL_FULL
        ends[user] = {"end": end, "bucket": Bucket(self.rate, self.burst, now), "seen": now}
        return OK

    def sweep(self, now=None):
        """Forget ends nobody has heard from for `idle` seconds, and channels left with none."""
        now = self._clock() if now is None else now
        self._swept = now
        for channel in list(self.channels):
            ends = self.channels[channel]
            for user in [u for u, e in ends.items() if now - e["seen"] > self.idle]:
                del ends[user]
            if not ends:
                del self.channels[channel]

    def drop_end(self, end):
        """A TCP connection closed: its ends go at once instead of waiting to be idle."""
        for channel in list(self.channels):
            ends = self.channels[channel]
            for user in [u for u, e in ends.items() if e["end"] == end]:
                del ends[user]
            if not ends:
                del self.channels[channel]
