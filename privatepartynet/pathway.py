"""Paths between members: how one PC's datagrams reach another's, whatever routers are in between.

The game network sends WireGuard datagrams to each member through a PeerPath. A path tries every way there is at once
and uses the best one that works, in this order:

    lan     the other PC's address on a shared network
    ipv6    its global IPv6 address
    upnp    a port its router opened (UPnP), or a public address
    punch   the public address its router gives our shared socket (STUN), reached by both sides sending at once
    friend  another member whose PC both can reach, forwarding for them (relayproto.py), if they allow it
    vps     a relay server the group runs itself (relay/README.md)

It starts on whatever answers first, keeps checking the others, and moves when a better one starts to answer (a relay
first, then the punched path a few seconds later, for example) or when the one in use stops. A relay only ever sees
WireGuard's ciphertext, and each pair of members meets on a relay channel only the two of them can work out.

Each member's PathManager has one UDP socket for all of this. What is sent on it:

    PROBE  0x30 from(16) nonce(8) mac(16) [padding]   is this way open? mac = HMAC-SHA256(pair key, the first 25 bytes)
    REPLY  0x31 from(16) nonce(8) mac(16) [padding]   yes; the same nonce back, under the replier's id, as long
    DATA   0x32 datagram                     a datagram for the game network

plus STUN, and relay messages (relayproto.py) both to the relays this PC uses and, as a friend relay, from members who
use it. Over a relay the same three travel inside SEND and DELIVER. The pair key comes from both members' identity keys
(X25519), so nobody else can make a path answer, or move it elsewhere. DATA is accepted only from an address or channel
that has proved itself that way. A way is used only once a probe padded to the size of the largest DATA has been
answered at that size, and it is checked at that size again every few probes: some links (IPv6 tunnels, some mobile
networks) carry small packets but drop large ones, and IPv6 routers never split a packet.

Candidates are what a member says about itself, sent to every other member through the party (the host passes them
on): [["key", dh, sign], [kind, address, port], ...] with the kinds above. A path needs the other side's "key" entry.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import queue
import random
import select
import socket
import threading
import time

from privatepartynet import relayproto as rp
from privatepartynet.identity import user_id_of, verify
from privatepartynet.relayclient import RelayClient

log = logging.getLogger("privatepartynet.pathway")

KINDS = ("lan", "ipv6", "upnp", "punch", "friend", "vps")
RANK = {kind: index for index, kind in enumerate(KINDS)}
DIRECT = ("lan", "ipv6", "upnp", "punch")
PROBE, REPLY, DATA = 0x30, 0x31, 0x32
PROBE_SIZE = 41
MAX_DATAGRAM = 1400  # what every path carries, the relays included; WireGuard's MTU is set to fit
FULL_SIZE = 1 + MAX_DATAGRAM  # a probe padded to the size of the largest DATA, to show the way carries that too
FULL_EVERY = 6  # every so many probes of a way that answers is full size
FULL_SECONDS = 45.0  # a way whose last full-size answer is older than this is not used
PROBE_FAST = 0.5  # seconds between probes of a way not proven yet, for FAST_SECONDS after the candidates arrive
FAST_SECONDS = 15.0
PROBE_SLOW = 5.0  # then this; ways that work but are not in use are checked as often
PROBE_ACTIVE = 2.0  # the way in use
DEAD_SECONDS = 7.0  # no answer for this long and a way is not used
STUN_SECONDS = 60.0
TICK = 0.1
FRIEND_MBPS = 8  # what a friend relay forwards for others at most, all together, unless its owner says otherwise
CONFIG_LABEL = b"PrivatePartyNet relay settings|"
PASS_LABEL = b"PrivatePartyNet relay pass|"
INVITE_KEY_LABEL = b"PrivatePartyNet invite relay key|"


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:16]


def kind_of(address):
    """The kind of a direct way, from the address it answered from: a home network is "lan", IPv6 is "ipv6"."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "punch"
    if ip.version == 6:
        return "ipv6"
    return "lan" if ip.is_private and not ip.is_loopback else "punch"


# --- the relay settings the owner's PC hands to members ----------------------------------------------------------------


def config_message(config):
    import json

    return CONFIG_LABEL + json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_relay_config(config, sign):
    """{"relay": config, "sig": hex}: the relay settings, signed by the owner's identity (`sign` is identity.sign)."""
    return {"relay": config, "sig": sign(config_message(config)).hex()}


def open_relay_config(signed, host_sign):
    """The relay settings from a signed bundle, or None when the signature is not the owner's (`host_sign`, hex)."""
    if not isinstance(signed, dict) or not isinstance(signed.get("relay"), dict) or not isinstance(signed.get("sig"), str):
        return None
    if not host_sign or not verify(host_sign, signed["sig"], config_message(signed["relay"])):
        return None
    return signed["relay"]


# --- friend relay passes ------------------------------------------------------------------------------------------------
#
# A friend relay accepts a member whose pass the owner signed: expiry (4) + user id (16) + the member's Ed25519 public key
# (32) + the owner's signature (64). Each BIND_PASS carries the pass and the member's own signature over the channel and
# the time, so a pass seen on the wire is no use without the member's private key, and no member can make passes.


def make_pass(owner_sign, user, member_public, lifetime=rp.TOKEN_SECONDS, now=None):
    """A pass for `user` (hex) holding `member_public` (hex or bytes), signed with `owner_sign` (identity.sign)."""
    import struct

    body = struct.pack("!I", int((now if now is not None else time.time()) + lifetime)) + bytes.fromhex(user) + \
        (bytes.fromhex(member_public) if isinstance(member_public, str) else bytes(member_public))
    if len(body) != 52:
        raise ValueError("a pass needs a 16-byte user id and a 32-byte public key")
    return body + owner_sign(PASS_LABEL + body)


def _pass_message(kind, channel, stamp, cookie, pass_):
    return rp.BIND_LABEL + bytes([kind]) + bytes(channel) + stamp + cookie + pass_


class PassCredential:
    """A member's pass for friend relays, with the private key that goes with it (`sign`, such as identity.sign)."""

    def __init__(self, pass_, sign):
        self.raw = bytes(pass_)
        if len(self.raw) != rp.PASS_BYTES:
            raise ValueError("not a relay pass")
        self.sign = sign
        self.expiry = rp.credential_expiry(self.raw)

    def bind(self, channel, cookie=b"", now=None):
        import struct

        stamp = struct.pack("!I", int(now if now is not None else time.time()))
        signature = self.sign(_pass_message(rp.BIND_PASS, channel, stamp, cookie, self.raw))
        return bytes([rp.BIND_PASS]) + bytes(channel) + self.raw + stamp + signature + cookie


class PassVerifier:
    """Checks BIND_PASS for a friend relay: a pass the owner (`owner_public`, hex) signed, and the member's signature."""

    kind = rp.BIND_PASS

    def __init__(self, owner_public):
        self.owner = bytes.fromhex(owner_public) if isinstance(owner_public, str) else bytes(owner_public)

    def tags(self):
        return {hashlib.sha256(self.owner).digest()[:8]}

    def check(self, channel, body, now):
        import struct

        if len(body) not in (rp.PASS_BODY, rp.PASS_BODY + rp.COOKIE_BYTES):
            return None
        pass_, stamp, signature, cookie = body[:116], body[116:120], body[120:184], body[184:]
        expiry, user, member = struct.unpack("!I", pass_[:4])[0], pass_[4:20], pass_[20:52]
        if expiry < now or abs(struct.unpack("!I", stamp)[0] - now) > rp.FRESH_SECONDS:
            return None
        if not verify(self.owner, pass_[52:], PASS_LABEL + pass_[:52]):
            return None
        if not verify(member, signature, _pass_message(rp.BIND_PASS, channel, stamp, cookie, pass_)):
            return None
        return user.hex(), expiry, hashlib.sha256(self.owner).digest()[:8], cookie


def invite_key(secret):
    """The key pair an invite's holder uses on friend relays, worked out from the invite's secret on both PCs:
    (sign function, public key bytes, user id hex)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = hashlib.sha256(INVITE_KEY_LABEL + bytes(secret)).digest()
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.sign, key.public_key().public_bytes_raw(), hashlib.sha256(b"PrivatePartyNet invite relay|" +
                                                                         bytes(secret)).digest()[:16].hex()


# --- the socket ---------------------------------------------------------------------------------------------------------


class UdpTransport:
    """One UDP port for IPv4 and, where the PC has it, IPv6. sendto() never waits: a full buffer drops the datagram."""

    def __init__(self, port=0):
        self.v4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.v4.bind(("0.0.0.0", port))
        self.v4.setblocking(False)
        self.port = self.v4.getsockname()[1]
        self.v6 = None
        try:
            v6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            v6.bind(("::", self.port))
            v6.setblocking(False)
            self.v6 = v6
        except OSError:
            pass

    def receive(self, timeout):
        """(data, (address, port)) or None after `timeout` seconds."""
        sockets = [s for s in (self.v4, self.v6) if s is not None]
        try:
            ready, _, _ = select.select(sockets, [], [], timeout)
        except (OSError, ValueError):
            return None
        for sock in ready:
            try:
                data, source = sock.recvfrom(65535)
            except (BlockingIOError, ConnectionResetError):
                continue  # Windows reports an earlier datagram that found no one as an error on the next read
            except OSError:
                continue
            return data, (source[0].split("%")[0], source[1])
        return None

    def sendto(self, data, address):
        sock = self.v6 if ":" in address[0] else self.v4
        if sock is None:
            return False
        try:
            sock.sendto(data, address[:2])
            return True
        except OSError:  # a full buffer (BlockingIOError) included: the datagram is dropped
            return False

    def close(self):
        for sock in (self.v4, self.v6):
            if sock is not None:
                sock.close()


# --- a path to one member -------------------------------------------------------------------------------------------


class _Route:
    """One way to a member: a direct address, or a channel on one relay."""

    __slots__ = ("kind", "address", "relay", "last_reply", "rtt", "next_probe", "nonces", "replies", "kind_hint", "full",
                 "probes")

    def __init__(self, kind, address=None, relay=None, kind_hint=None):
        self.kind, self.address, self.relay = kind, address, relay
        self.kind_hint = kind_hint  # for a punch address: "easy" or "hard", as the other member's STUN check said
        self.full = None  # when a full-size probe was last answered
        self.probes = 0
        self.last_reply = None
        self.rtt = 1.0
        self.next_probe = 0.0
        self.nonces = {}  # nonce -> when it was sent
        self.replies = 0

    def alive(self, now):
        return self.last_reply is not None and now - self.last_reply < DEAD_SECONDS

    def usable(self, now):
        """Alive, and lately shown to carry the largest datagram."""
        return self.alive(now) and self.full is not None and now - self.full < FULL_SECONDS


class PeerPath:
    """The way to one member. Made by PathManager.path_to; the same object for as long as the member is known.

    `kind` is the way in use ("lan", "ipv6", "upnp", "punch", "friend" or "vps"), or "" while none answers yet."""

    def __init__(self, manager, peer_id):
        self.peer_id = peer_id
        self.kind = ""
        self._manager = manager
        self._receiver = None
        self._on_change = None
        self.closed = False
        self.stats = {"sent": 0, "received": 0, "dropped": 0}

    def send(self, datagram):
        """Send one datagram (at most MAX_DATAGRAM bytes). Never waits: with no way open, or a full buffer, it is dropped."""
        if self.closed or len(datagram) > MAX_DATAGRAM or not self._manager._send(self, bytes(datagram)):
            self.stats["dropped"] += 1
            return
        self.stats["sent"] += 1

    def set_receiver(self, fn):
        """fn(datagram) for each datagram from the member. It runs on the manager's own thread, so it must be quick."""
        self._receiver = fn

    def set_on_change(self, fn):
        """fn(new_kind) when the path moves to a better or a worse way."""
        self._on_change = fn

    def close(self):
        if not self.closed:
            self.closed = True
            self._manager._close(self)

    def _deliver(self, datagram):
        receiver = self._receiver
        if receiver is not None and not self.closed:
            self.stats["received"] += 1
            try:
                receiver(datagram)
            except Exception:
                log.exception("The receiver of a path failed")

    def _changed(self, kind):
        self.kind = kind
        callback = self._on_change
        if callback is not None:
            try:
                callback(kind)
            except Exception:
                log.exception("A path's change handler failed")


class _Peer:
    """What the manager knows about one member besides the PeerPath it hands out."""

    def __init__(self, path):
        self.path = path
        self.key = None  # the pair key
        self.channel = None  # this pair's relay channel
        self.routes = {}  # ("direct", address) or ("relay", relay key) -> _Route
        self.active = None
        self.updated = 0.0
        self.friend_offers = set()  # (address, port) where this member relays for others


class PathManager:
    """Every path from this PC to the other members, on one UDP socket.

    `settings` is the app's settings: "path_port" (0 for any), "friend_relay" (True to forward for others, which is
    off unless the person turned it on) and "friend_relay_mbps" (the most it forwards for others, all together). `relay_config` is what the owner's PC hands
    each member (see set_relay_config). The keyword arguments stand in for the network in tests: a `transport`, the
    `addresses` [(kind, address)] to offer, the `stun_servers`; `probe_network` False means no router or STUN calls."""

    def __init__(self, identity, settings, relay_config=None, *, transport=None, addresses=None, stun_servers=None,
                 probe_network=True, clock=time.monotonic):
        self.identity = identity
        self.settings = dict(settings or {})
        self._clock = clock
        self._transport = transport or UdpTransport(int(self.settings.get("path_port") or 0))
        self._addresses = addresses
        self._probe_network = probe_network
        if stun_servers is None:
            from privatepartynet.connect import STUN_SERVERS

            stun_servers = STUN_SERVERS if probe_network else ()
        self._stun_names = list(stun_servers)
        self._stun_addresses = []
        self._stun_asked = {}  # transaction -> server address
        self._stun_seen = {}  # server address -> (address, port)
        self._stun_next = 0.0
        self.public = None  # (address, port, nat) as STUN servers see this socket
        self._upnp = None
        self._upnp_result = None
        self._lock = threading.RLock()
        self._peers = {}  # member id -> _Peer
        self._by_address = {}  # address -> member id, for addresses that proved themselves
        self._by_channel = {}  # relay channel -> member id
        self._relays = {}  # relay key -> RelayClient
        self._relay_kinds = {}  # relay key -> "friend" or "vps"
        self._relay_owner = {}  # relay key -> the member whose PC it is (friend relays)
        self._relay_config = {}
        self._pass = None  # this member's PassCredential for friend relays
        self._owner = None  # the owner's public key, which friend passes are checked against
        self._core = None  # the friend relay: forwarding for others
        self._candidates = []
        self._on_candidates = None
        self._stop = threading.Event()
        self.set_relay_config(relay_config)
        self._refresh_candidates()
        self._threads = [threading.Thread(target=self._receive_loop, name="privatepartynet-paths-in", daemon=True),
                         threading.Thread(target=self._tick_loop, name="privatepartynet-paths", daemon=True)]
        for thread in self._threads:
            thread.start()
        if probe_network:
            threading.Thread(target=self._open_router, name="privatepartynet-paths-upnp", daemon=True).start()
        threading.Thread(target=self._resolve_stun, name="privatepartynet-paths-stun", daemon=True).start()

    @property
    def port(self):
        return self._transport.port

    # --- what the game network calls ---------------------------------------------------------------------------

    def path_to(self, peer_id, candidates):
        """The path to member `peer_id`, at once. It starts on the best way that answers and moves as better ones do.
        Calling again with newer candidates updates the same path."""
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None or peer.path.closed:
                peer = self._peers[peer_id] = _Peer(PeerPath(self, peer_id))
            self._learn_locked(peer, candidates or [])
            return peer.path

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        with self._lock:
            for relay in self._relays.values():
                relay.close()
            self._relays.clear()
            for peer in self._peers.values():
                peer.path.closed = True
        if self._upnp is not None:
            self._upnp.unmap()
        self._transport.close()

    # --- what the party exchange calls -------------------------------------------------------------------------

    def local_candidates(self):
        """What this PC says about itself, for the other members."""
        with self._lock:
            return [list(c) for c in self._candidates]

    def set_on_candidates(self, fn):
        """fn(candidates) whenever local_candidates() changes (a router port opens, STUN answers, a relay key arrives)."""
        self._on_candidates = fn

    def set_relay_config(self, relay_config):
        """The relays this member may use, from the owner's PC (open_relay_config gives it from the signed bundle):
        {"vps": [{"host", "port", "tls_port", "pin", "credential": hex}], "friend": {"owner": hex, "pass": hex}}. None
        or {} is no relays. Call it again with the newer settings the owner's PC hands out before these expire."""
        config = relay_config if isinstance(relay_config, dict) else {}
        with self._lock:
            self._relay_config = config
            wanted = {}
            for entry in config.get("vps") or []:
                try:
                    relay = {"host": str(entry["host"]), "port": int(entry["port"]), "tls_port": int(entry.get("tls_port") or 0),
                             "pin": str(entry.get("pin") or ""),
                             "credential": rp.HmacCredential(bytes.fromhex(entry["credential"]))}
                except (KeyError, TypeError, ValueError):
                    continue
                wanted[(relay["host"], relay["port"])] = relay
            for key in [k for k, kind in self._relay_kinds.items() if kind == "vps" and k not in wanted]:
                self._drop_relay_locked(key)
            for key, relay in wanted.items():
                current = self._relays.get(key)
                if current is not None:
                    current.relay.update(relay)
                    for channel in list(current.channels):
                        current.want(channel, relay["credential"])
                    continue
                self._add_relay_locked(key, relay, "vps")
            friend = config.get("friend") if isinstance(config.get("friend"), dict) else {}
            try:
                self._owner = bytes.fromhex(friend["owner"])
            except (KeyError, TypeError, ValueError):
                self._owner = None
            try:
                self._pass = PassCredential(bytes.fromhex(friend["pass"]), self.identity.sign) if self._owner else None
            except (KeyError, TypeError, ValueError):
                self._pass = None  # this member may relay for others, but has no pass to use friend relays itself
            self._set_core_locked()
            for key, kind in list(self._relay_kinds.items()):
                if kind != "friend":
                    continue
                if self._pass is None:
                    self._drop_relay_locked(key)
                    continue
                self._relays[key].relay["credential"] = self._pass
                for channel in list(self._relays[key].channels):
                    self._relays[key].want(channel, self._pass)
            for peer in self._peers.values():
                self._friend_relays_locked(peer)
            self._bind_all_locked()
        self._refresh_candidates()

    def set_friend_relay(self, allowed, mbps=None):
        """Allow or stop forwarding for others, and how much."""
        with self._lock:
            self.settings["friend_relay"] = bool(allowed)
            if mbps is not None:
                self.settings["friend_relay_mbps"] = mbps
            self._set_core_locked()
        self._refresh_candidates()

    def state(self):
        """For the app's screens: each member's way in use, this PC's own, and whom it is relaying for now."""
        with self._lock:
            return {"public": self.public, "port": self.port, "friend_relay": self._core is not None,
                    "relayed": dict(self._core.stats) if self._core else None,
                    "through": sorted({u for ends in self._core.channels.values() for u in ends}) if self._core else [],
                    "peers": {pid: {"kind": p.path.kind, "rtt": round(p.active.rtt * 1000) if p.active else None,
                                    "ways": sorted({r.kind for r in p.routes.values() if r.usable(self._clock())})}
                              for pid, p in self._peers.items()}}

    def friend_offers(self):
        """(address, port) of every member known to relay for others, this PC included: what an invite can carry."""
        with self._lock:
            found = [tuple(c[1:3]) for c in self._candidates if c[0] == "friend"]
            for peer in self._peers.values():
                found += [o for o in sorted(peer.friend_offers) if o not in found]
            return found

    def problems(self):
        """Members this PC has found no way to after trying for FAST_SECONDS, each with the reason in plain words and
        what would help: [{"peer", "why"}]."""
        now = self._clock()
        found = []
        with self._lock:
            mine = self.public[2] if self.public else "unknown"
            relays = bool(self._relays)
            for peer_id, peer in self._peers.items():
                if peer.path.kind or now - peer.updated < FAST_SECONDS:
                    continue
                if peer.key is None:
                    found.append({"peer": peer_id, "why": "Their PC has not sent its addresses yet."})
                    continue
                theirs = next((r.kind_hint for r in peer.routes.values() if getattr(r, "kind_hint", None)), "unknown")
                if mine == "hard" and theirs == "hard":
                    why = "Both of your routers give a new port for every connection, so no direct way can open."
                elif "hard" in (mine, theirs):
                    why = "One of your routers gives a new port for every connection and the other lets nothing in."
                else:
                    why = "Neither PC can be reached from the other's network."
                if relays:
                    why += " The relays did not answer either."
                else:
                    why += (" A member whose PC others can reach can turn on relaying for others in the control panel, "
                            "or the owner can add a relay server.")
                found.append({"peer": peer_id, "why": why})
        return found

    # --- relays --------------------------------------------------------------------------------------------------

    def _add_relay_locked(self, key, relay, kind, owner=None):
        client = RelayClient(relay, lambda channel, payload, key=key: self._from_relay(key, channel, payload),
                             self._transport.sendto, clock=self._clock)
        self._relays[key] = client
        self._relay_kinds[key] = kind
        if owner is not None:
            self._relay_owner[key] = owner
        return client

    def _drop_relay_locked(self, key):
        client = self._relays.pop(key, None)
        self._relay_kinds.pop(key, None)
        self._relay_owner.pop(key, None)
        for peer in self._peers.values():
            peer.routes.pop(("relay", key), None)
            if peer.active is not None and peer.active.relay is client:
                peer.active = None
        if client is not None:
            client.close()

    def _set_core_locked(self):
        allowed = self.settings.get("friend_relay") is True and self._owner is not None
        if not allowed:
            self._core = None
            return
        rate = int(max(1, float(self.settings.get("friend_relay_mbps") or FRIEND_MBPS)) * 125_000)
        if self._core is None:
            self._core = rp.RelayCore([PassVerifier(self._owner)], user_rate=rate, total_rate=rate, clock=self._clock)
        else:
            self._core.set_verifiers([PassVerifier(self._owner)])
            self._core.set_total_rate(rate)

    def _friend_relays_locked(self, peer):
        """Relays offered by this member, for paths to the others."""
        if self._pass is None:
            return
        for address, port in peer.friend_offers:
            key = (address, port)
            if key not in self._relays:
                self._add_relay_locked(key, {"host": address, "port": port, "credential": self._pass}, "friend",
                                       owner=peer.path.peer_id)

    def _bind_all_locked(self):
        """Every pair's channel is wanted on every relay, except a friend relay on the PC of the member it leads to."""
        for peer in self._peers.values():
            if peer.channel is None:
                continue
            for key, client in self._relays.items():
                if self._relay_owner.get(key) == peer.path.peer_id:
                    continue
                client.want(peer.channel)
                if ("relay", key) not in peer.routes:
                    peer.routes[("relay", key)] = _Route(self._relay_kinds[key], relay=client)

    def _from_relay(self, key, channel, payload):
        with self._lock:
            peer_id = self._by_channel.get(channel)
            client = self._relays.get(key)
        if peer_id is not None and client is not None:
            self._path_message(payload, None, client, channel)

    # --- candidates ----------------------------------------------------------------------------------------------

    def _learn_locked(self, peer, candidates):
        peer_id = peer.path.peer_id
        offers = set()
        for item in candidates:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            kind, address, port = item[0], item[1], item[2]
            if kind == "key":
                self._set_key_locked(peer, address, port)
                continue
            try:
                ipaddress.ip_address(address)
                port = int(port)
                if not 0 < port < 65536:
                    continue
            except (ValueError, TypeError):
                continue
            if kind in DIRECT:
                route = peer.routes.get(("direct", (address, port)))
                hint = item[3] if kind == "punch" and len(item) > 3 and item[3] in ("easy", "hard", "unknown") else None
                if route is None:
                    peer.routes[("direct", (address, port))] = _Route(kind, address=(address, port), kind_hint=hint)
                elif hint:
                    route.kind_hint = hint
            elif kind == "friend":
                offers.add((address, port))
        for key in [("relay", (a, p)) for a, p in peer.friend_offers - offers]:
            if self._relay_owner.get(key[1]) == peer_id:
                self._drop_relay_locked(key[1])
        peer.friend_offers = offers
        peer.updated = self._clock()
        for route in peer.routes.values():
            if not route.alive(peer.updated):
                route.next_probe = 0.0  # new candidates: try everything again quickly
        self._friend_relays_locked(peer)
        self._bind_all_locked()

    def _set_key_locked(self, peer, dh, sign):
        if peer.key is not None:
            return
        try:
            if user_id_of(sign, dh) != peer.path.peer_id:
                log.warning("A member's keys do not match their id; no path to them")
                return
            peer.key = self.identity.shared_key(dh, b"path")
        except (ValueError, TypeError):
            return
        peer.channel = rp.channel_for("path", peer.key)
        self._by_channel[peer.channel] = peer.path.peer_id

    def _own_addresses(self):
        if self._addresses is not None:
            return [(kind, address) for kind, address in self._addresses]
        from privatepartynet.connect import global_ipv6_addresses
        from privatepartynet.netinfo import LOOPBACK, lan_addresses

        found = [("lan", a) for a in lan_addresses() if a != LOOPBACK]
        if self._transport.v6 is not None:
            found += [("ipv6", a) for a in global_ipv6_addresses()]
        return found

    def _refresh_candidates(self):
        port = self._transport.port
        found = [["key", self.identity.dh_public.hex(), self.identity.sign_public.hex()]]
        try:
            addresses = self._own_addresses()
        except OSError:
            addresses = []
        found += [[kind, address, port] for kind, address in addresses]
        upnp = self._upnp_result
        if upnp and upnp.get("ok") and upnp.get("external_ip"):
            from privatepartynet.connect import classify_public_ip

            if classify_public_ip(upnp["external_ip"]) == "public":
                found.append(["upnp", upnp["external_ip"], upnp["external_port"]])
        if self.public is not None:
            found.append(["punch", self.public[0], self.public[1], self.public[2]])  # the router's kind, for problems()
        with self._lock:
            if self._core is not None:
                # Offer to relay where others can reach this PC: IPv6, an open port, and a router that keeps one public
                # port for this socket (others may still be stopped by it; whoever cannot get an answer moves on).
                reachable = [c for c in found if c[0] in ("ipv6", "upnp")]
                if self.public is not None and self.public[2] == "easy":
                    reachable.append(["punch", self.public[0], self.public[1]])
                found += [["friend", c[1], c[2]] for c in reachable]
            changed = found != self._candidates
            self._candidates = found
        callback = self._on_candidates
        if changed and callback is not None:
            try:
                callback([list(c) for c in found])
            except Exception:
                log.exception("The candidates handler failed")

    def _open_router(self):
        from privatepartynet.connect import UpnpMapping

        mapping = UpnpMapping(self._transport.port, protocol="UDP")
        try:
            result = mapping.map()
        except Exception:
            log.debug("Opening a UDP port on the router failed", exc_info=True)
            return
        if self._stop.is_set():
            mapping.unmap()
            return
        self._upnp, self._upnp_result = mapping, result
        self._refresh_candidates()

    def _resolve_stun(self):
        found = []
        for host, port in self._stun_names:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
            except OSError:
                continue
            found.append(infos[0][4][:2])
        self._stun_addresses = found

    def _ask_stun(self, now):
        from privatepartynet.connect import stun_request

        self._stun_next = now + STUN_SECONDS
        self._stun_seen = {}
        self._stun_asked = {}
        for server in self._stun_addresses:
            request, transaction = stun_request()
            self._stun_asked[transaction] = server
            self._transport.sendto(request, server)

    def _stun_answer(self, data):
        from privatepartynet.connect import parse_stun

        server = self._stun_asked.get(bytes(data[8:20]))
        found = parse_stun(data)
        if server is None or found is None:
            return
        self._stun_seen[server] = found
        seen = list(self._stun_seen.values())
        ports = {port for _, port in seen}
        nat = "unknown" if len(seen) < 2 else ("easy" if len(ports) == 1 else "hard")
        public = (seen[0][0], seen[0][1], nat)
        if public != self.public:
            self.public = public
            self._refresh_candidates()

    # --- sending and receiving -----------------------------------------------------------------------------------

    def _send(self, path, datagram):
        with self._lock:
            peer = self._peers.get(path.peer_id)
            route = peer.active if peer is not None and peer.path is path else None
        if route is None:
            return False
        return self._send_on(route, peer, bytes([DATA]) + datagram)

    def _send_on(self, route, peer, data):
        if route.relay is not None:
            return route.relay.send(peer.channel, data)
        return self._transport.sendto(data, route.address)

    def _close(self, path):
        with self._lock:
            peer = self._peers.get(path.peer_id)
            if peer is None or peer.path is not path:
                return
            del self._peers[path.peer_id]
            if peer.channel is not None:
                self._by_channel.pop(peer.channel, None)
                for client in self._relays.values():
                    client.drop(peer.channel)
            for address in [a for a, pid in self._by_address.items() if pid == path.peer_id]:
                del self._by_address[address]
            for key in [k for k, owner in self._relay_owner.items() if owner == path.peer_id]:
                self._drop_relay_locked(key)

    def _receive_loop(self):
        while not self._stop.is_set():
            got = self._transport.receive(0.2)
            if got is None:
                continue
            data, source = got
            if not data:
                continue
            try:
                self._received(data, source)
            except Exception:
                log.exception("Handling a datagram failed")

    def _received(self, data, source):
        first = data[0]
        if first in (PROBE, REPLY, DATA):
            self._path_message(data, source)
        elif first in (rp.BOUND, rp.DELIVER):
            with self._lock:
                client = next((c for c in self._relays.values() if c.matches(source)), None)
            if client is not None:
                client.feed(data, source)
        elif first in (rp.BIND, rp.BIND_PASS, rp.SEND):
            core = self._core
            if core is not None:
                with self._lock:
                    out = core.handle(data, ("udp", source))
                for (_, address), message in out:
                    self._transport.sendto(message, address)
        elif first in (0x00, 0x01) and len(data) >= 20:
            self._stun_answer(data)

    def _path_message(self, data, source, relay=None, channel=None):
        kind = data[0]
        now = self._clock()
        if kind == DATA:
            with self._lock:
                peer_id = self._by_channel.get(channel) if relay is not None else self._by_address.get(source)
                peer = self._peers.get(peer_id)
            if peer is not None:
                peer.path._deliver(data[1:])
            return
        if kind not in (PROBE, REPLY) or len(data) not in (PROBE_SIZE, FULL_SIZE):
            return
        sender, nonce = data[1:17].hex(), data[17:25]
        with self._lock:
            peer = self._peers.get(sender)
            if peer is None or peer.key is None or not hmac.compare_digest(data[25:41], _hmac(peer.key, data[:25])):
                return
            if relay is not None and channel != peer.channel:
                return
            if kind == PROBE:
                head = bytes([REPLY]) + bytes.fromhex(self.identity.user_id) + nonce
                answer = head + _hmac(peer.key, head)
                answer += bytes(len(data) - len(answer))  # as long as the probe, so the way back is tested at that size
                if relay is None and ("direct", source) not in peer.routes:
                    # It reached us from an address we did not know of: probe it back. Only a reply to our own probe
                    # lets DATA in from there, since a probe could be a copy someone sent again from elsewhere.
                    peer.routes[("direct", source)] = _Route(kind_of(source[0]), address=source)
                route = None
            else:
                key = ("relay", relay.key) if relay is not None else ("direct", source)
                route = peer.routes.get(key)
                sent = route.nonces.pop(nonce, None) if route is not None else None
                if sent is None:
                    return
                route.last_reply = now
                route.rtt = (now - sent) if route.replies == 0 else route.rtt * 0.75 + (now - sent) * 0.25
                route.replies += 1
                if len(data) == FULL_SIZE:
                    route.full = now
                elif route.full is None:
                    route.next_probe = now  # it answers: check at once that it carries full-size datagrams too
                if relay is None:
                    self._by_address[source] = sender
        if kind == PROBE:
            if relay is not None:
                relay.send(channel, answer)
            else:
                self._transport.sendto(answer, source)
            return
        self._choose(peer, now)

    def _probe(self, peer, route, now):
        nonce = os.urandom(8)
        head = bytes([PROBE]) + bytes.fromhex(self.identity.user_id) + nonce
        route.nonces[nonce] = now
        if len(route.nonces) > 16:
            route.nonces.pop(next(iter(route.nonces)))
        probe = head + _hmac(peer.key, head)
        # Small until the way answers at all, then full size until it has answered at full size, then now and then.
        if route.replies and (route.full is None or route.probes % FULL_EVERY == 0):
            probe += bytes(FULL_SIZE - len(probe))
        route.probes += 1
        self._send_on(route, peer, probe)

    def _choose(self, peer, now):
        """Move to the best way that answers. A way of the same rank as the one in use does not replace it."""
        with self._lock:
            alive = [r for r in peer.routes.values() if r.usable(now)]
            if not alive:
                return
            best = min(alive, key=lambda r: (RANK[r.kind], r.rtt))
            active = peer.active
            if active is not None and active.usable(now) and RANK[active.kind] <= RANK[best.kind]:
                return
            if best is active:
                return
            peer.active = best
            changed = best.kind != peer.path.kind
        if changed:
            log.info("Path to %s: %s", peer.path.peer_id[:8], best.kind)
            peer.path._changed(best.kind)

    def _tick_loop(self):
        while not self._stop.wait(TICK):
            try:
                self._tick()
            except Exception:
                log.exception("The paths' timer failed")

    def _tick(self):
        now = self._clock()
        if self._stun_addresses and now >= self._stun_next:
            self._ask_stun(now)
        with self._lock:
            relays = list(self._relays.values())
            peers = list(self._peers.values())
        for client in relays:
            client.tick(now)
        for peer in peers:
            if peer.key is None:
                continue
            with self._lock:
                routes = list(peer.routes.values())
            for route in routes:
                if route.next_probe > now:
                    continue
                if route.relay is not None and not route.relay.is_bound(peer.channel):
                    route.next_probe = now + PROBE_FAST
                    continue
                if route is peer.active:
                    interval = PROBE_ACTIVE
                elif route.alive(now) or now - peer.updated > FAST_SECONDS:
                    interval = PROBE_SLOW
                else:
                    interval = PROBE_FAST
                route.next_probe = now + interval * random.uniform(0.9, 1.1)
                self._probe(peer, route, now)
            active = peer.active
            if active is not None and not active.usable(now):
                self._choose_after_loss(peer, now)

    def _choose_after_loss(self, peer, now):
        with self._lock:
            alive = [r for r in peer.routes.values() if r.usable(now)]
            if not alive:
                return  # keep sending on the last way: it may come back, and nothing else answers either
            best = min(alive, key=lambda r: (RANK[r.kind], r.rtt))
            peer.active = best
            changed = best.kind != peer.path.kind
        if changed:
            log.info("Path to %s: %s (the way in use stopped answering)", peer.path.peer_id[:8], best.kind)
            peer.path._changed(best.kind)


# --- in memory, for tests ---------------------------------------------------------------------------------------------


class LoopbackHub:
    """Where LoopbackPathManagers find each other. Delivery happens on the hub's own thread, as on a real network."""

    def __init__(self):
        self.managers = {}
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="privatepartynet-loopback-paths", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            path, datagram = item
            path._deliver(datagram)

    def close(self):
        self._queue.put(None)


_DEFAULT_HUB = None


class LoopbackPathManager:
    """A PathManager in memory: managers on the same hub reach each other at once, with no sockets. Paths start on
    `kind` ("lan" by default). switch(kind) moves every path (or one member's) and calls their change handlers, and
    `loss` drops that share of datagrams at random. For the game network's tests."""

    def __init__(self, identity, settings=None, relay_config=None, *, hub=None, kind="lan", loss=0.0):
        global _DEFAULT_HUB
        if hub is None:
            _DEFAULT_HUB = _DEFAULT_HUB or LoopbackHub()
            hub = _DEFAULT_HUB
        self.user_id = identity if isinstance(identity, str) else identity.user_id
        self.identity = None if isinstance(identity, str) else identity
        self.settings = dict(settings or {})
        self.relay_config = relay_config
        self.hub, self.kind, self.loss = hub, kind, loss
        self.paths = {}
        self._on_candidates = None
        self.stopped = False
        hub.managers[self.user_id] = self

    def path_to(self, peer_id, candidates):
        path = self.paths.get(peer_id)
        if path is None or path.closed:
            path = self.paths[peer_id] = PeerPath(self, peer_id)
            path.kind = self.kind
        path.candidates = list(candidates or [])
        return path

    def local_candidates(self):
        return [["loopback", self.user_id, 0]]

    def set_on_candidates(self, fn):
        self._on_candidates = fn

    def set_relay_config(self, relay_config):
        self.relay_config = relay_config

    def set_friend_relay(self, allowed, mbps=None):
        self.settings["friend_relay"] = bool(allowed)
        if mbps is not None:
            self.settings["friend_relay_mbps"] = mbps

    @property
    def port(self):
        return 0

    def problems(self):
        return []

    def friend_offers(self):
        return []

    def state(self):
        return {"public": None, "port": 0, "friend_relay": False, "relayed": None, "through": [],
                "peers": {pid: {"kind": p.kind, "rtt": 0, "ways": [p.kind]} for pid, p in self.paths.items()}}

    def switch(self, kind, peer_id=None):
        """Move every path (or the one to `peer_id`) to `kind`, as a real manager does when a better way answers."""
        for pid, path in list(self.paths.items()):
            if (peer_id is None or pid == peer_id) and path.kind != kind and not path.closed:
                path._changed(kind)
        if peer_id is None:
            self.kind = kind

    def _send(self, path, datagram):
        other = self.hub.managers.get(path.peer_id)
        if self.stopped or other is None or other.stopped:
            return False
        back = other.paths.get(self.user_id)
        if back is None or back.closed:
            return False
        if self.loss and random.random() < self.loss:
            return True  # lost on the way, as a datagram can be
        self.hub._queue.put((back, datagram))
        return True

    def _close(self, path):
        if self.paths.get(path.peer_id) is path:
            del self.paths[path.peer_id]

    def stop(self):
        self.stopped = True
        for path in list(self.paths.values()):
            path.closed = True
        if self.hub.managers.get(self.user_id) is self:
            del self.hub.managers[self.user_id]
