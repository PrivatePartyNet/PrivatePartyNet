"""The game network: each server is also a small private network, so games that play over a LAN work between members.

How it fits together:

* Each server has its own /24 network in 10.0.0.0/8, chosen from the server id and moved off any network this PC is
  already on. The host is .1; each member keeps the same address on that server every time (remembered by their id).
* Members reach each other directly over WireGuard, a full mesh: every member has a WireGuard key pair for each server
  (kept sealed by the vault), and a packet goes straight to the member it is for, never through the host. WireGuard
  only accepts from each member packets with that member's own address as the sender (their /32 is all they are
  allowed), so nobody can pretend to be someone else on the game network.
* The host keeps the list of who is on (the Directory): address, WireGuard public key and the ways each member can be
  reached. It signs the list and hands it to every member over their game network link (route /net), and sends it
  again the moment it changes, so a member who leaves, is removed or is banned is dropped by everyone at once.
* WireGuard has no socket of its own. Its datagrams travel the app's own paths to each member (pathway.py: straight
  across the LAN, through a hole punched in both routers, through a friend's PC, or through the relay).
* On each PC a virtual network adapter (Wintun) gives Windows and the games an ordinary network card with that address.
  Making one needs administrator rights (measured: error 5 as a normal user), so a small helper started with Windows'
  permission prompt holds the adapter and runs WireGuard on it; see gameclient.py and wghelper/.
* Only IPv4 to this server's network (and broadcast and multicast) is carried. Anything else Windows sends on the adapter
  (IPv6, traffic for the internet) is dropped in the helper, so the game network never becomes a route to anywhere else.
  WireGuard alone would drop broadcasts and multicasts (how games find each other on a LAN); the helper sends a copy to
  each member instead (wghelper/fanout.go).
"""

import hashlib
import ipaddress
import json
import os
import socket
import struct
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from privatepartynet.identity import verify

AVOID_SECOND_OCTET = {0, 1, 2, 5, 8, 10, 20, 50, 100, 168, 200, 254, 255}  # 10.x networks routers and VPNs often use
MULTICAST = ipaddress.ip_network("224.0.0.0/4")
LIMITED_BROADCAST = ipaddress.ip_address("255.255.255.255")
KEEPALIVE_SECONDS = 15.0  # the host's link to each member sends something at least this often
RELAY_REFRESH_SECONDS = 1800.0  # the list is sent again this often anyway, with fresh relay settings (they last hours)
MAX_CANDIDATES = 24  # ways to reach one member, as pathway.py lists them
MAX_CANDIDATES_BYTES = 4096
ROSTER_LABEL = b"PrivatePartyNet game roster|"
KEY_FOLDER = "wireguard"


# Windows' own services that party members cannot reach on this PC unless its owner opens them, group by group, in the
# control panel. The game network is marked Private so games can host, and Windows opens these to Private networks too;
# the helper drops members' packets to them (wghelper/services.go has the ports). Games never need them. The setting
# belongs to this PC and is never sent to the party.
SERVICE_GROUPS = [
    {"id": "sharing", "label": "File and printer sharing",
     "detail": "Members could open the folders and printers this PC shares."},
    {"id": "remote_desktop", "label": "Remote Desktop",
     "detail": "Members could ask to sign in to this PC, if Remote Desktop is turned on in Windows."},
    {"id": "remote_management", "label": "Remote management",
     "detail": "Administration tools on members' PCs (WinRM, remote procedure calls) could reach this PC."},
    {"id": "discovery", "label": "Network discovery",
     "detail": "Members' PCs could see this PC and its media and devices in their Network folder."},
]
SERVICE_IDS = tuple(group["id"] for group in SERVICE_GROUPS)


class NetError(Exception):
    pass


# --- addresses --------------------------------------------------------------------------------------------------


def subnet_for(server_id, in_use=()):
    """The /24 a server's game network uses: 10.a.b.0/24 from a hash of the server id, skipping second octets that are
    common on home networks and any network in `in_use` (this PC's own networks). Same server id, same network."""
    taken = [ipaddress.ip_network(n, strict=False) for n in in_use]
    for attempt in range(256):
        digest = hashlib.sha256(f"PrivatePartyNet game network|{server_id}|{attempt}".encode("utf-8")).digest()
        a, b = 16 + digest[0] % 224, digest[1]
        if a in AVOID_SECOND_OCTET or b in (0, 255):
            continue
        network = ipaddress.ip_network(f"10.{a}.{b}.0/24")
        if not any(network.overlaps(other) for other in taken):
            return network
    raise NetError("no free network could be found for this server")


def local_networks():
    """The IPv4 networks this PC is on, each at its real size (NordVPN's NordLynx, for one, is a /16: 10.5.0.0/16 on
    the author's PC). Read from the adapters with psutil; falls back to treating each address as a /24."""
    found = set()
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for entry in addresses:
                if entry.family == socket.AF_INET and entry.address and entry.netmask:
                    try:
                        found.add(ipaddress.ip_network(f"{entry.address}/{entry.netmask}", strict=False))
                    except ValueError:
                        continue
    except Exception:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.add(ipaddress.ip_network(f"{info[4][0]}/24", strict=False))
        except OSError:
            return []
    return sorted(found)


# --- packets ------------------------------------------------------------------------------------------------------


def parse(packet):
    """(source, destination) of an IPv4 packet, or None for anything that is not a well-formed IPv4 packet."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    header = (packet[0] & 0x0F) * 4
    total = struct.unpack(">H", packet[2:4])[0]
    if header < 20 or len(packet) < header or total < header or total > len(packet):
        return None
    return ipaddress.IPv4Address(packet[12:16]), ipaddress.IPv4Address(packet[16:20])


def is_group(destination, network):
    """True for a packet to everyone: this network's broadcast address, 255.255.255.255, or multicast."""
    return destination == network.broadcast_address or destination == LIMITED_BROADCAST or destination in MULTICAST


def make_packet(source, destination, payload=b"", protocol=17):
    """An IPv4 packet (UDP by default) with a correct header checksum, for tests and keep-alive probes."""
    total = 20 + len(payload)
    header = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, protocol, 0,
                         ipaddress.IPv4Address(source).packed, ipaddress.IPv4Address(destination).packed)
    checksum = 0
    for index in range(0, 20, 2):
        checksum += (header[index] << 8) + header[index + 1]
    while checksum >> 16:
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
    header = header[:10] + struct.pack(">H", ~checksum & 0xFFFF) + header[12:]
    return header + payload


# --- WireGuard keys -----------------------------------------------------------------------------------------------


def new_private_key():
    """A WireGuard private key, hex (WireGuard keys are X25519 keys)."""
    raw = X25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                    serialization.NoEncryption())
    return raw.hex()


def public_key(private_hex):
    key = X25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def is_key(value):
    try:
        return isinstance(value, str) and len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def load_key(vault, folder, server_key):
    """This PC's WireGuard private key for one server (hex), made the first time and kept sealed by the vault. A key of
    its own for each server means one server's members cannot link this PC to its place on another server."""
    name = hashlib.sha256(f"PrivatePartyNet wireguard|{server_key}".encode("utf-8")).hexdigest()[:24]
    path = Path(folder) / KEY_FOLDER / f"{name}.bin"
    aad = f"wireguard|{server_key}".encode("utf-8")
    if path.exists():
        return vault.unseal("wireguard", path.read_bytes(), aad).decode("ascii")
    private = new_private_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(vault.seal("wireguard", private.encode("ascii"), aad))
    os.replace(temp, path)
    return private


# --- who is on: the host's directory ------------------------------------------------------------------------------


def clean_candidates(value):
    """The ways to reach a member as pathway.py lists them ([kind, address, port] and the like), kept only when it is a
    short list of short lists of strings and numbers. Anything else is an empty list."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_CANDIDATES]:
        if isinstance(item, list) and 0 < len(item) <= 4 and all(
                isinstance(part, (str, int)) and not isinstance(part, bool) and len(str(part)) <= 255 for part in item):
            out.append(list(item))
    return out if len(json.dumps(out)) <= MAX_CANDIDATES_BYTES else []


class Directory:
    """Who is on one server's game network: each member's address, WireGuard public key and ways to be reached. The
    host keeps one, members join it through their link, and everyone is sent the whole list when it changes. Thread
    safe.

    `assignments` maps a member's permanent id to the last byte of their address, and is kept by the caller (the party
    saves it with the server), so a member has the same address every time."""

    def __init__(self, network, assignments=None, on_change=None, host_user=None):
        self.network = ipaddress.ip_network(network)
        self.host_user = host_user
        self._assign = dict(assignments or {})
        self._on_change = on_change or (lambda assignments: None)
        self._on = {}  # user id -> {"user", "address", "key", "candidates"}
        self._changed = threading.Condition()
        self.version = 0

    @property
    def host_address(self):
        return self.network.network_address + 1

    def address_for(self, user):
        """This member's address on the network, given the first time and kept. The host is .1."""
        if user is None or user == self.host_user:
            return self.host_address
        with self._changed:
            if user not in self._assign:
                used = set(self._assign.values())
                free = next((n for n in range(2, 255) if n not in used), None)
                if free is None:
                    raise NetError("this game network is full")
                self._assign[user] = free
                changed = dict(self._assign)
            else:
                changed = None
            last = self._assign[user]
        if changed is not None:
            self._on_change(changed)
        return self.network.network_address + last

    def assignments(self):
        with self._changed:
            return dict(self._assign)

    def join(self, user, key, candidates=()):
        """Put a member on the network with their WireGuard public key. Returns their address. A second join replaces
        the first (the member reconnected, or turned the game network on again with the same key)."""
        if not is_key(key):
            raise NetError("that is not a WireGuard key")
        address = self.address_for(user)
        with self._changed:
            if any(entry["key"] == key and other != user for other, entry in self._on.items()):
                raise NetError("that WireGuard key belongs to someone else")
            self._on[user] = {"user": user, "address": str(address), "key": key,
                              "candidates": clean_candidates(list(candidates))}
            self._bump_locked()
        return address

    def set_candidates(self, user, candidates):
        """A member's ways to be reached changed (a router opened a port, a relay answered)."""
        with self._changed:
            entry = self._on.get(user)
            cleaned = clean_candidates(candidates)
            if entry is None or entry["candidates"] == cleaned:
                return False
            entry["candidates"] = cleaned
            self._bump_locked()
            return True

    def leave(self, user, key=None):
        """Take a member off the network: everyone drops them with the next list. With `key`, only if it is still the
        key they joined with (a late leave from an old link must not remove the new one)."""
        with self._changed:
            entry = self._on.get(user)
            if entry is None or (key is not None and entry["key"] != key):
                return False
            del self._on[user]
            self._bump_locked()
            return True

    def online(self):
        with self._changed:
            return sorted(entry["address"] for entry in self._on.values())

    def peers(self):
        with self._changed:
            return [dict(entry, candidates=[list(c) for c in entry["candidates"]])
                    for entry in sorted(self._on.values(), key=lambda e: ipaddress.ip_address(e["address"]))]

    def wait(self, version, timeout):
        """Wait until the list is newer than `version`, for at most `timeout` seconds. Returns the current version."""
        with self._changed:
            self._changed.wait_for(lambda: self.version != version, timeout)
            return self.version

    def _bump_locked(self):
        self.version += 1
        self._changed.notify_all()


# --- the signed list ----------------------------------------------------------------------------------------------


def roster_body(server_id, network, version, peers):
    """The list as the host signs it: the server, its network, a number that only grows, and everyone on."""
    return json.dumps({"server": server_id, "network": str(network), "seq": version,
                       "peers": [{"user": p["user"], "address": p["address"], "key": p["key"],
                                  "candidates": p["candidates"]} for p in peers]},
                      sort_keys=True, separators=(",", ":"))


def sign_roster(body, sign):
    """{"roster", "sig"} for a list body. `sign` is the host identity's signing call, or None (the signature is then
    empty and members rely on the link's pinned certificate alone)."""
    return {"roster": body, "sig": sign(ROSTER_LABEL + body.encode("utf-8")).hex() if sign else ""}


def open_roster(signed, host_sign="", server_id="", network=None):
    """The list from a signed one, checked: signed by the host when the host's signing key is known, for this server
    and this network. Raises NetError otherwise."""
    try:
        body = signed["roster"]
        if host_sign and not verify(host_sign, signed.get("sig") or "", ROSTER_LABEL + body.encode("utf-8")):
            raise NetError("the list of members is not signed by the host")
        roster = json.loads(body)
        if server_id and roster["server"] != server_id:
            raise NetError("the list of members is for another server")
        if network is not None and roster["network"] != str(network):
            raise NetError("the list of members is for another network")
        net = ipaddress.ip_network(roster["network"])
        peers = []
        for p in roster["peers"]:
            address = ipaddress.ip_address(p["address"])
            if address not in net or not is_key(p["key"]) or not isinstance(p["user"], str):
                raise NetError("the list of members has an entry that is not right")
            peers.append({"user": p["user"], "address": str(address), "key": p["key"],
                          "candidates": clean_candidates(p.get("candidates"))})
        return {"server": roster["server"], "network": str(net), "seq": int(roster["seq"]), "peers": peers}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NetError(f"the list of members could not be read ({exc})") from None
