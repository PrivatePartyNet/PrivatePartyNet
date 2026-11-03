"""The relay: what passes between a PC and a relay, and the relay's forwarding rules. Standard library only, because the
relay server on a rented Linux machine (relay/ppn_relay.py) runs this same file without the rest of the app.

A relay forwards datagrams between two PCs that cannot reach each other. It is either a server the group runs itself (a
"VPS relay", set up from relay/README.md) or a party member whose PC others can reach (a "friend relay", see
pathway.py). Both speak this protocol and keep the same rules.

How two PCs meet on a relay: each sends BIND for the same 16-byte channel, which both derive from a secret only the two
of them hold, with proof that they may use the relay. The relay then hands every SEND from one end to the other as
DELIVER. It never knows who the two are beyond the user id in their credentials, and what it forwards is WireGuard or
QUIC, already encrypted end to end: a relay cannot read or change the traffic, only drop it.

Credentials. Nothing that travels in a BIND can be used again elsewhere:

* VPS relay: the owner's PC gives each member, over the party's own TLS connection, expiry (4 bytes) + user id (16) +
  a key: HMAC-SHA256(secret, label + expiry + user). The member never sends the key; each BIND carries expiry, user, the
  time, and an HMAC under the key over the channel and the time. The relay works the key out again from the secret
  (it needs no list of members) and accepts only a fresh time, the right channel, and an expiry still ahead. The secret
  never leaves the owner's PC. TURN credentials for coturn (voice and screens) come from the same secret in coturn's
  own form (use-auth-secret).
* Friend relay: the owner's PC signs a pass for each member (expiry, user id and that member's own public key, under
  the owner's identity key); BIND carries the pass and the member's signature over the channel and the time (see
  pathway.py, which has the signatures). Members can check passes but cannot make them.

A new end, or an end that moves to another address, must first show it can receive there: the relay answers a BIND
from an unknown UDP address with a cookie, and only a BIND that echoes it binds. A TCP connection has shown that already.

Messages (one per UDP datagram; on the TCP fallback each is preceded by its length in two bytes):

    BIND        0x20 channel(16) expiry(4) user(16) time(4) proof(16) [cookie(16)]       VPS relay
    BIND_PASS   0x24 channel(16) pass(116) time(4) signature(64) [cookie(16)]            friend relay
    BOUND       0x21 channel(16) status(1) [cookie(16)]   the answer; 0 is yes, CHECK comes with the cookie to echo
    SEND        0x22 channel(16) payload                  a PC sends a datagram to the other end
    DELIVER     0x23 channel(16) payload                  the relay hands it on

A PC sends BIND again every REBIND_SECONDS. A relay keeps at most two ends per channel, one per user id. Each end has
its own rate limit, each user one more over all their channels, and the relay one over everything; an end whose
credentials expire, or whose key the relay no longer has, goes at once, and a channel nobody has used for IDLE_SECONDS
is forgotten.
"""

import base64
import hashlib
import hmac
import os
import struct
import time

BIND, BOUND, SEND, DELIVER, BIND_PASS = 0x20, 0x21, 0x22, 0x23, 0x24
OK, BAD_TOKEN, CHANNEL_FULL, RELAY_FULL, CHECK = 0, 1, 2, 3, 4
STATUS_TEXT = {OK: "ok", BAD_TOKEN: "the relay did not accept the credentials", CHANNEL_FULL: "the channel is in use",
               RELAY_FULL: "the relay is full", CHECK: "the relay is checking the address"}
CHANNEL_BYTES = 16
CREDENTIAL_BYTES = 52  # expiry, user, key: what a member holds for a VPS relay
PASS_BYTES = 116  # expiry, user, public key, owner's signature: a friend relay pass
COOKIE_BYTES = 16
BIND_BODY = 4 + 16 + 4 + 16
PASS_BODY = PASS_BYTES + 4 + 64
HEADER = 1 + CHANNEL_BYTES  # what a SEND or DELIVER adds to a datagram
MAX_PAYLOAD = 1408  # the largest datagram a relay forwards: 1400 for the paths, plus their own one-byte marker, with room
MAX_MESSAGE = HEADER + max(MAX_PAYLOAD, PASS_BODY + COOKIE_BYTES)
TOKEN_LABEL = b"PrivatePartyNet relay|"
BIND_LABEL = b"PrivatePartyNet bind|"
TOKEN_SECONDS = 6 * 3600  # a member's credentials; the owner's PC hands out new ones well before then
FRESH_SECONDS = 60  # how far a BIND's time may be from the relay's clock
REBIND_SECONDS = 15.0  # how often a PC sends BIND again; it also keeps routers open
IDLE_SECONDS = 60.0
DEFAULT_RATE = 1_000_000  # bytes a second for one end of one channel
DEFAULT_BURST = 256 * 1024
DEFAULT_USER_RATE = 2_000_000  # bytes a second for one user over all their channels
DEFAULT_TOTAL_RATE = 12_500_000  # bytes a second for the whole relay (100 Mbit/s)
MAX_CHANNELS = 4096
MAX_CHANNELS_PER_USER = 32  # a party has 16 people: one channel to each of the others, and room for joins
DEFAULT_UDP_PORT = 3479  # coturn keeps 3478
DEFAULT_TLS_PORT = 443


class RelayError(Exception):
    pass


# --- keys, credentials and channels ------------------------------------------------------------------------------------


def secret_key(secret):
    """The HMAC key for a relay secret as typed (text) or as bytes."""
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def _user_bytes(user):
    raw = bytes.fromhex(user) if isinstance(user, str) else bytes(user)
    if len(raw) != 16:
        raise ValueError("a user id is 16 bytes")
    return raw


def _member_key(secret, expiry, user):
    return hmac.new(secret_key(secret), TOKEN_LABEL + expiry + user, hashlib.sha256).digest()


def member_credential(secret, user, lifetime=TOKEN_SECONDS, now=None):
    """What the owner's PC gives member `user` (hex or 16 bytes) for a VPS relay holding `secret`: expiry, user and the
    member's own key. It must travel only over the party's TLS connection: whoever has it can use the relay until it
    expires."""
    expiry = struct.pack("!I", int((now if now is not None else time.time()) + lifetime))
    user = _user_bytes(user)
    return expiry + user + _member_key(secret, expiry, user)


def credential_expiry(credential):
    return struct.unpack("!I", bytes(credential[:4]))[0] if len(credential) >= 4 else 0


def _bind_mac(key, kind, channel, stamp, cookie):
    return hmac.new(key, BIND_LABEL + bytes([kind]) + bytes(channel) + stamp + cookie, hashlib.sha256).digest()[:16]


def bind(channel, credential, cookie=b"", now=None):
    """A BIND for a VPS relay, made from a member credential (member_credential). Fresh for FRESH_SECONDS only."""
    credential = bytes(credential)
    if len(credential) != CREDENTIAL_BYTES:
        raise ValueError("not a relay credential")
    expiry, user, key = credential[:4], credential[4:20], credential[20:]
    stamp = struct.pack("!I", int(now if now is not None else time.time()))
    return bytes([BIND]) + bytes(channel) + expiry + user + stamp + _bind_mac(key, BIND, channel, stamp, cookie) + cookie


class HmacCredential:
    """A member's credential for a VPS relay, ready to make BINDs (what RelayClient uses)."""

    def __init__(self, raw):
        self.raw = bytes(raw)
        if len(self.raw) != CREDENTIAL_BYTES:
            raise ValueError("not a relay credential")
        self.expiry = credential_expiry(self.raw)

    def bind(self, channel, cookie=b"", now=None):
        return bind(channel, self.raw, cookie, now)


class HmacVerifier:
    """Checks VPS-relay BINDs against the relay's secrets (more than one while the owner moves to a new secret)."""

    kind = BIND

    def __init__(self, secrets_):
        self.keys = [secret_key(s) for s in secrets_]

    def tags(self):
        return {hashlib.sha256(k).digest()[:8] for k in self.keys}

    def check(self, channel, body, now):
        """(user hex, expiry, key tag, cookie) for a good BIND body, else None."""
        if len(body) not in (BIND_BODY, BIND_BODY + COOKIE_BYTES):
            return None
        expiry, user, stamp, proof, cookie = body[:4], body[4:20], body[20:24], body[24:40], body[40:]
        if struct.unpack("!I", expiry)[0] < now or abs(struct.unpack("!I", stamp)[0] - now) > FRESH_SECONDS:
            return None
        for key in self.keys:
            if hmac.compare_digest(proof, _bind_mac(_member_key(key, expiry, user), BIND, channel, stamp, cookie)):
                return user.hex(), struct.unpack("!I", expiry)[0], hashlib.sha256(key).digest()[:8], cookie
        return None


def turn_credentials(secret, user, lifetime=24 * 3600, now=None):
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


def bound(channel, status, cookie=b""):
    return bytes([BOUND]) + bytes(channel) + bytes([status]) + cookie


def send(channel, payload):
    return bytes([SEND]) + bytes(channel) + bytes(payload)


def deliver(channel, payload):
    return bytes([DELIVER]) + bytes(channel) + bytes(payload)


def parse(data):
    """(type, channel, rest) of a relay message, or None when it is not one."""
    if len(data) < HEADER or len(data) > MAX_MESSAGE or data[0] not in (BIND, BOUND, SEND, DELIVER, BIND_PASS):
        return None
    kind, channel, rest = data[0], bytes(data[1:HEADER]), bytes(data[HEADER:])
    if kind == BIND and len(rest) not in (BIND_BODY, BIND_BODY + COOKIE_BYTES):
        return None
    if kind == BIND_PASS and len(rest) not in (PASS_BODY, PASS_BODY + COOKIE_BYTES):
        return None
    if kind == BOUND and len(rest) not in (1, 1 + COOKIE_BYTES):
        return None
    return kind, channel, rest


def is_relay_message(data):
    return bool(data) and data[0] in (BIND, BOUND, SEND, DELIVER, BIND_PASS)


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


def _bucket(rate, now):
    """A bucket for `rate` bytes a second holding a quarter of a second's worth (never less than 16 datagrams)."""
    return Bucket(rate, max(rate // 4, 16 * MAX_PAYLOAD), now) if rate else None


class RelayCore:
    """The forwarding table of one relay, apart from any socket: `handle(data, end)` takes what arrived from `end` (any
    hashable value that names a way back: ("udp", address), or ("tcp", n) for a connection) and returns what to send,
    as [(end, bytes)].

    `verifiers` check BINDs (HmacVerifier for a VPS relay; pathway.PassVerifier for a friend relay). `rate`/`burst`
    limit each end of each channel, `user_rate` each user over all their channels, and `total_rate` everything (None
    for no limit), which is also how a friend relay keeps to the bandwidth its owner allows."""

    def __init__(self, verifiers, rate=DEFAULT_RATE, burst=DEFAULT_BURST, user_rate=DEFAULT_USER_RATE, total_rate=None,
                 max_channels=MAX_CHANNELS, max_per_user=MAX_CHANNELS_PER_USER, idle=IDLE_SECONDS,
                 clock=time.monotonic, wall=time.time):
        self.verifiers = list(verifiers)
        self.rate, self.burst, self.user_rate = rate, burst, user_rate
        self.max_channels, self.max_per_user, self.idle = max_channels, max_per_user, idle
        self._clock, self._wall = clock, wall
        self.total = None
        self.set_total_rate(total_rate)
        self.channels = {}  # channel -> {user: {"end", "bucket", "seen", "expiry", "tag", "stamp"}}
        self.users = {}  # user -> their bucket over all channels
        self.stats = {"forwarded": 0, "bytes": 0, "limited": 0, "refused": 0, "dropped": 0, "checked": 0}
        self._cookie_key = os.urandom(32)
        self._swept = clock()

    def set_total_rate(self, total_rate):
        """Limit everything together to `total_rate` bytes a second, or None for no limit."""
        self.total = _bucket(total_rate, self._clock())

    def set_verifiers(self, verifiers):
        """New credentials checks (a new secret, a new owner key). Ends bound under a key that is gone go at once."""
        self.verifiers = list(verifiers)
        tags = set().union(*(v.tags() for v in self.verifiers)) if self.verifiers else set()
        self._drop(lambda e: e["tag"] not in tags)

    def _cookie(self, end, channel, user, bucket):
        return hmac.new(self._cookie_key, repr(end).encode() + channel + user.encode() + struct.pack("!I", bucket),
                        hashlib.sha256).digest()[:COOKIE_BYTES]

    def _cookie_ok(self, cookie, end, channel, user, now):
        bucket = int(now // 30)
        return any(hmac.compare_digest(cookie, self._cookie(end, channel, user, b)) for b in (bucket, bucket - 1))

    def handle(self, data, end, routable=False):
        """`routable` is True when `end` has already shown it receives what is sent to it (a TCP connection)."""
        now = self._clock()
        if now - self._swept > self.idle / 4:
            self.sweep(now)
        message = parse(data)
        if message is None:
            self.stats["dropped"] += 1
            return []
        kind, channel, rest = message
        if kind in (BIND, BIND_PASS):
            return [(end, self._bind(kind, channel, rest, end, routable, now))]
        if kind != SEND:
            self.stats["dropped"] += 1
            return []
        ends = self.channels.get(channel)
        sender = next((u for u, e in ends.items() if e["end"] == end), None) if ends else None
        if sender is None or len(rest) > MAX_PAYLOAD:
            self.stats["dropped"] += 1
            return []
        me = ends[sender]
        if me["expiry"] < self._wall():
            del ends[sender]  # the credentials ran out while the end was in use
            self.stats["dropped"] += 1
            return []
        me["seen"] = now
        other = next((e for u, e in ends.items() if u != sender), None)
        if other is None:
            self.stats["dropped"] += 1
            return []
        size = len(rest) + HEADER
        user_bucket = self.users.get(sender)
        if not me["bucket"].take(size, now) or (user_bucket is not None and not user_bucket.take(size, now)) \
                or (self.total is not None and not self.total.take(size, now)):
            self.stats["limited"] += 1
            return []
        self.stats["forwarded"] += 1
        self.stats["bytes"] += size
        return [(other["end"], deliver(channel, rest))]

    def _bind(self, kind, channel, body, end, routable, now):
        wall = self._wall()
        checked = None
        for verifier in self.verifiers:
            if verifier.kind == kind:
                checked = verifier.check(channel, body, wall)
                if checked is not None:
                    break
        if checked is None:
            self.stats["refused"] += 1
            return bound(channel, BAD_TOKEN)
        user, expiry, tag, cookie = checked
        ends = self.channels.get(channel)
        current = ends.get(user) if ends else None
        if current is not None and current["end"] == end:
            current["seen"], current["expiry"], current["tag"] = now, expiry, tag  # a refresh from where it already is
            return bound(channel, OK)
        if not routable and not (cookie and self._cookie_ok(cookie, end, channel, user, now)):
            # A new end, or one moving: prove first that this address is really where the BIND came from.
            self.stats["checked"] += 1
            return bound(channel, CHECK, self._cookie(end, channel, user, int(now // 30)))
        if current is not None:
            current["end"], current["seen"], current["expiry"], current["tag"] = end, now, expiry, tag
            return bound(channel, OK)
        if ends is None:
            if len(self.channels) >= self.max_channels:
                self.stats["refused"] += 1
                return bound(channel, RELAY_FULL)
            if sum(1 for e in self.channels.values() if user in e) >= self.max_per_user:
                self.stats["refused"] += 1
                return bound(channel, RELAY_FULL)
            ends = self.channels[channel] = {}
        if len(ends) >= 2:
            self.stats["refused"] += 1
            return bound(channel, CHANNEL_FULL)
        ends[user] = {"end": end, "bucket": Bucket(self.rate, self.burst, now), "seen": now, "expiry": expiry, "tag": tag}
        if self.user_rate and user not in self.users:
            self.users[user] = _bucket(self.user_rate, now)
        return bound(channel, OK)

    def _drop(self, gone):
        for channel in list(self.channels):
            ends = self.channels[channel]
            for user in [u for u, e in ends.items() if gone(e)]:
                del ends[user]
            if not ends:
                del self.channels[channel]
        active = {u for ends in self.channels.values() for u in ends}
        for user in [u for u in self.users if u not in active]:
            del self.users[user]

    def sweep(self, now=None):
        """Forget ends nobody has heard from for `idle` seconds or whose credentials ran out, and channels left with none."""
        now = self._clock() if now is None else now
        wall = self._wall()
        self._swept = now
        self._drop(lambda e: now - e["seen"] > self.idle or e["expiry"] < wall)

    def drop_end(self, end):
        """A TCP connection closed: its ends go at once instead of waiting to be idle."""
        self._drop(lambda e: e["end"] == end)

    def has_end(self, end):
        return any(e["end"] == end for ends in self.channels.values() for e in ends.values())
