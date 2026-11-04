"""Reaching a party from outside the network, and letting others reach yours, with no company server in the middle.

What this module does:

* works out how this PC can be reached: its LAN addresses, any global IPv6 address, the public address that STUN
  servers see (and whether the router keeps one mapping per socket, which decides whether hole punching can work), and
  an automatic router port-open (UPnP) where the router offers it;
* turns that into an invite (the server's name, its password, where to try, and the host's certificate fingerprint);
* asks to join a party over HTTPS at each address in turn, checking the host's certificate against the fingerprint,
  and waits for the owner or an admin to answer.

What it cannot do alone: reach a host that sits behind carrier-grade NAT with no IPv6 and no router mapping. That needs
hole punching between two PCs that exchange their public addresses at the same moment, which is the next stage; see
the connection layer in SPEC-2026-11-01. This module reports that case plainly instead of pretending.

Every network call here has a timeout, and nothing runs after stop().
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

from privatepartynet.netinfo import LOOPBACK, lan_addresses
from privatepartynet.server import ask_message

log = logging.getLogger("privatepartynet.connect")

STUN_SERVERS = (("stun.l.google.com", 19302), ("stun1.l.google.com", 19302), ("stun.cloudflare.com", 3478),
                ("stun.nextcloud.com", 3478))
STUN_MAGIC = 0x2112A442
INVITE_PREFIX = "ppn1-"
MAX_CANDIDATES = 8
MAX_RELAYS = 5  # the relay server at two addresses, and three members who relay for others
ASK_TIMEOUT = 5.0
ANSWER_WAIT = 300.0
POLL_SECONDS = 1.5
SHARED_SPACE = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT addresses (RFC 6598), which Starlink uses


class ConnectError(Exception):
    """Plain words for the person: what failed and, where known, what to do about it."""


# --- STUN: the address the outside world sees -----------------------------------------------------------------


def stun_request():
    """(request bytes, transaction id) for one STUN binding request."""
    transaction = os.urandom(12)
    return struct.pack("!HHI", 0x0001, 0, STUN_MAGIC) + transaction, transaction


def parse_stun(data, transaction=None):
    """(address, port) from a STUN binding answer, or None. With `transaction`, only the answer to that request."""
    if len(data) < 20 or data[4:8] != struct.pack("!I", STUN_MAGIC):
        return None
    if transaction is not None and data[8:20] != transaction:
        return None  # not the answer to our question
    _, length, _ = struct.unpack("!HHI", data[:8])
    transaction = data[8:20]
    position = 20
    while position + 4 <= min(20 + length, len(data)):
        kind, size = struct.unpack("!HH", data[position:position + 4])
        body = data[position + 4:position + 4 + size]
        if kind == 0x0020 and len(body) >= 8:
            port_x = struct.unpack("!H", body[2:4])[0] ^ (STUN_MAGIC >> 16)
            if body[1] == 1:
                address = struct.unpack("!I", body[4:8])[0] ^ STUN_MAGIC
                return socket.inet_ntoa(struct.pack("!I", address)), port_x
            if len(body) < 20:
                return None
            raw = bytes(a ^ b for a, b in zip(body[4:20], struct.pack("!I", STUN_MAGIC) + transaction))
            return socket.inet_ntop(socket.AF_INET6, raw), port_x
        position += 4 + size + (-size % 4)
    return None


def _stun_binding(sock, host, port, family):
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    request, transaction = stun_request()
    sock.sendto(request, infos[0][4])
    data, _ = sock.recvfrom(2048)
    return parse_stun(data, transaction)


def stun_probe(servers=STUN_SERVERS, timeout=2.0, family=socket.AF_INET, local_port=0):
    """Ask several public STUN servers, from one socket, which address and port they see.

    Returns {"public": ip or None, "port": port or None, "nat": "easy" | "hard" | "unknown", "answers": n}.
    "easy" means every server saw the same port (the router keeps one mapping per socket), so two PCs can punch
    through to each other. "hard" means each server saw a different port. "unknown" is fewer than two answers."""
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    seen = []
    try:
        sock.bind(("", local_port))
        for host, port in servers:
            try:
                found = _stun_binding(sock, host, port, family)
            except (OSError, struct.error):
                continue
            if found:
                seen.append(found)
    except OSError:
        pass
    finally:
        sock.close()
    if not seen:
        return {"public": None, "port": None, "nat": "unknown", "answers": 0}
    ports = {port for _, port in seen}
    nat = "unknown" if len(seen) < 2 else ("easy" if len(ports) == 1 else "hard")
    return {"public": seen[0][0], "port": seen[0][1], "nat": nat, "answers": len(seen)}


# --- addresses of this PC -------------------------------------------------------------------------------------


TEREDO = ipaddress.ip_network("2001::/32")
SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def global_ipv6_addresses(entries=None):
    """This PC's IPv6 addresses that other networks can in principle reach, best first: stable addresses before
    temporary (privacy) ones, which are kept since they are often all a network leaves, and the old 6to4 tunnel range
    last, since it rarely works. Link-local, unique-local, loopback and Teredo are left out. Most carrier-grade NAT
    providers give real IPv6 with no NAT at all, so these are how two members behind CGNAT usually reach each other.

    `entries` is [(address, preferred lifetime)], from Windows' own list when it can be read, else the host name's."""
    from privatepartynet.netinfo import ipv6_unicast

    if entries is None:
        entries = ipv6_unicast()
    if entries is None:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        except OSError:
            infos = []
        entries = [(info[4][0], 0) for info in infos]
    ranked = []
    for position, (text, lifetime) in enumerate(entries):
        text = text.split("%")[0]
        try:
            address = ipaddress.IPv6Address(text)
        except ValueError:
            continue
        six_to_four = address in SIX_TO_FOUR
        if six_to_four:
            if not address.sixtofour.is_global:  # (Python counts all of 6to4 as private) a 6to4 of a private IPv4
                continue
        elif address.is_link_local or address.is_loopback or address.is_private or address.is_multicast:
            continue
        if address in TEREDO or address.ipv4_mapped:
            continue
        ranked.append((six_to_four, -lifetime, position, text))
    found = []
    for *_, text in sorted(ranked):
        if text not in found:
            found.append(text)
    return found


def classify_public_ip(address):
    """What kind of address the outside world sees: "shared" (carrier-grade NAT), "private" or "public"."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if ip in SHARED_SPACE:
        return "shared"
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return "private"
    return "public"


# --- UPnP: ask the router to open the port ---------------------------------------------------------------------

SSDP_ADDRESS = ("239.255.255.250", 1900)
SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
          "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode("ascii")
SERVICE_TYPES = ("urn:schemas-upnp-org:service:WANIPConnection:1", "urn:schemas-upnp-org:service:WANIPConnection:2",
                 "urn:schemas-upnp-org:service:WANPPPConnection:1")


def _soap(url, service, action, arguments, timeout):
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">'
            + "".join(f"<{key}>{value}</{key}>" for key, value in arguments)
            + f"</u:{action}></s:Body></s:Envelope>").encode("utf-8")
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        connection.request("POST", parts.path or "/", body=body, headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _find_gateway(search_address, timeout):
    """(control url, service type, gateway ip) of the first router that answers, or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    deadline = time.monotonic() + timeout
    try:
        sock.sendto(SEARCH, search_address)
        while time.monotonic() < deadline:
            try:
                data, source = sock.recvfrom(4096)
            except socket.timeout:
                break
            match = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not match:
                continue
            location = match.group(1).decode("ascii", "replace")
            found = _read_description(location, timeout)
            if found:
                return found + (source[0],)
    except OSError:
        return None
    finally:
        sock.close()
    return None


def _read_description(location, timeout):
    parts = urllib.parse.urlsplit(location)
    if parts.scheme != "http" or not parts.hostname:
        return None
    try:
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
        connection.request("GET", parts.path + (("?" + parts.query) if parts.query else ""))
        response = connection.getresponse()
        text = response.read(262144)
        connection.close()
        root = ET.fromstring(text)
    except (OSError, http.client.HTTPException, ET.ParseError):
        return None
    for service in root.iter():
        if not service.tag.endswith("}service") and service.tag != "service":
            continue
        values = {child.tag.split("}")[-1]: (child.text or "").strip() for child in service}
        if values.get("serviceType") in SERVICE_TYPES and values.get("controlURL"):
            return urllib.parse.urljoin(location, values["controlURL"]), values["serviceType"]
    return None


def _local_address_towards(host):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


class UpnpMapping:
    """Ask the router to forward one port (TCP, or UDP for the paths between members) to this PC, and take the request
    back when asked.

    `map()` returns {"ok", "external_ip", "external_port", "reason"}. The mapping has a lease, which a background
    thread renews until `unmap()`; the thread ends with unmap() or after `max_seconds`, whichever comes first."""

    def __init__(self, port, search_address=SSDP_ADDRESS, timeout=2.0, lease=3600, max_seconds=24 * 3600, protocol="TCP"):
        self.port = port
        self.protocol = protocol
        self._search = search_address
        self._timeout = timeout
        self._lease = lease
        self._max_seconds = max_seconds
        self._gateway = None
        self._internal = None
        self._stop = threading.Event()
        self._thread = None
        self.result = {"ok": False, "external_ip": None, "external_port": None, "reason": "not tried"}

    @property
    def gateway_address(self):
        """The router's address on this network, once it has answered (None before)."""
        return self._gateway[2] if self._gateway else None

    def _add(self, lease):
        url, service = self._gateway[0], self._gateway[1]
        status, _ = _soap(url, service, "AddPortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", self.port), ("NewProtocol", self.protocol),
            ("NewInternalPort", self.port), ("NewInternalClient", self._internal), ("NewEnabled", 1),
            ("NewPortMappingDescription", "PrivatePartyNet"), ("NewLeaseDuration", lease)], self._timeout)
        return status == 200

    def map(self):
        found = _find_gateway(self._search, self._timeout)
        if found is None:
            self.result = {"ok": False, "external_ip": None, "external_port": None,
                           "reason": "The router did not answer a port-open request (UPnP may be off or unsupported)."}
            return self.result
        self._gateway = found
        self._internal = _local_address_towards(found[2]) or (lan_addresses() or [None])[0]
        try:
            opened = self._add(self._lease) or self._add(0)  # some routers refuse a timed lease
            external = None
            if opened:
                status, text = _soap(found[0], found[1], "GetExternalIPAddress", [], self._timeout)
                match = re.search(rb"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", text)
                external = match.group(1).decode("ascii", "replace") if status == 200 and match else None
        except (OSError, http.client.HTTPException) as exc:
            self.result = {"ok": False, "external_ip": None, "external_port": None, "reason": f"The router refused: {exc}"}
            return self.result
        if not opened:
            self.result = {"ok": False, "external_ip": None, "external_port": None,
                           "reason": "The router refused to open the port."}
            return self.result
        self.result = {"ok": True, "external_ip": external or None, "external_port": self.port, "reason": ""}
        self._thread = threading.Thread(target=self._renew, name="privatepartynet-upnp", daemon=True)
        self._thread.start()
        return self.result

    def _renew(self):
        started = time.monotonic()
        while not self._stop.wait(max(60, self._lease // 2)):
            if time.monotonic() - started > self._max_seconds:
                break
            try:
                self._add(self._lease)
            except (OSError, http.client.HTTPException):
                log.debug("Renewing the router port-open failed", exc_info=True)

    def unmap(self):
        self._stop.set()
        if self._gateway is None or not self.result.get("ok"):
            return
        try:
            _soap(self._gateway[0], self._gateway[1], "DeletePortMapping",
                  [("NewRemoteHost", ""), ("NewExternalPort", self.port), ("NewProtocol", self.protocol)], self._timeout)
        except (OSError, http.client.HTTPException):
            log.debug("Removing the router port-open failed", exc_info=True)
        self.result = {**self.result, "ok": False, "reason": "closed"}


# --- what this PC can be reached on ----------------------------------------------------------------------------


def gather(port, upnp=None, stun=None, ipv6=None, lan=None):
    """Everything known about how to reach a party on `port` of this PC, as a report:

    {"candidates": [[kind, address, port], ...], "nat": ..., "public": ..., "upnp": {...}, "cgnat": bool,
     "reachable": "yes" | "maybe" | "no", "advice": text}

    The pieces can be passed in (the tests do), and are looked up otherwise."""
    lan = lan if lan is not None else [a for a in lan_addresses() if a != LOOPBACK]
    ipv6 = ipv6 if ipv6 is not None else global_ipv6_addresses()
    stun = stun if stun is not None else stun_probe()
    upnp = upnp if upnp is not None else {"ok": False, "external_ip": None, "external_port": None, "reason": "not tried"}
    candidates = [["lan", address, port] for address in lan]
    candidates += [["ipv6", address, port] for address in ipv6]
    public = upnp.get("external_ip") if upnp.get("ok") else None
    if upnp.get("ok") and public and classify_public_ip(public) == "public":
        candidates.append(["public", public, upnp["external_port"]])
    stun_ip = stun.get("public")
    cgnat = False
    if stun_ip:
        if classify_public_ip(stun_ip) == "shared":
            cgnat = True
        if upnp.get("ok") and public and public != stun_ip:
            cgnat = True  # the router's own outside address is not the one the internet sees: another NAT sits above it
        if classify_public_ip(public or "") in ("private", "shared"):
            cgnat = True
    reachable, advice = _advice(upnp, ipv6, cgnat, stun)
    return {"candidates": candidates[:MAX_CANDIDATES], "nat": stun.get("nat", "unknown"), "public": stun_ip,
            "upnp": upnp, "cgnat": cgnat, "ipv6": ipv6, "reachable": reachable, "advice": advice}


def _advice(upnp, ipv6, cgnat, stun):
    if ipv6 and cgnat:
        return "yes", ("This internet connection shares one IPv4 address between many customers (carrier-grade NAT), but "
                       "it also has IPv6, which has no such sharing: members with IPv6 reach this PC directly over it, "
                       "and the game network goes over IPv6 whenever both ends have it. If your router has a firewall for "
                       "IPv6 (the standard Starlink router blocks it and cannot be changed), the invite still gets through "
                       "it, because both PCs send at the same moment.")
    if ipv6:
        return "yes", ("People with IPv6 can reach this PC directly. If your router has a firewall for IPv6 (the standard "
                       "Starlink router blocks it and cannot be changed), allow this port or put the router in bypass mode.")
    if upnp.get("ok") and not cgnat:
        return "yes", "Your router opened the port, so people outside your network can join with the invite."
    if cgnat:
        return "no", ("This internet connection shares one public address between many customers (carrier-grade NAT, as on "
                      "Starlink), and no router setting can open it. People on your own network can join. People outside "
                      "join with the invite through both routers, which works unless their router is of the same strict "
                      "kind; then only IPv6 or a relay helps (a member who relays for others, or your group's relay "
                      "server).")
    if stun.get("public") is None:
        return "maybe", "No public address could be found. Check the internet connection."
    if stun.get("nat") == "easy":
        return "maybe", ("Your router did not open the port by itself, but the invite still works through it: both PCs "
                         "send at the same moment, which suits routers like yours. Forwarding the port is faster.")
    return "maybe", "Your router did not open the port by itself. Forward the port in the router."


# --- invites ---------------------------------------------------------------------------------------------------


def certificate_fingerprint(pem_text):
    """SHA-256 of a PEM certificate's DER bytes, in hex: what an invite pins and a join checks."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem_text)).hexdigest()


def encode_invite(name, password, candidates, fingerprint, server_id="", host_sign="", secret="", relays=()):
    """`secret` (hex) lets the joiner find the host through the DHT when no address answers (punch.py). `relays`
    (Party.invite_relays) are where the two can meet when no direct way works at all."""
    data = {"v": 1, "n": str(name)[:40], "p": str(password), "c": [list(c) for c in candidates][:MAX_CANDIDATES],
            "f": fingerprint, "s": server_id, "k": host_sign, "r": secret}
    if relays:
        data["y"] = [list(r) for r in relays][:MAX_RELAYS]
    packed = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 9)
    check = hashlib.sha256(packed).digest()[:3]
    return INVITE_PREFIX + base64.urlsafe_b64encode(packed + check).decode("ascii").rstrip("=")


def decode_invite(code):
    """The invite as a dict. Raises ConnectError, in words, for anything that is not a good invite."""
    text = "".join(str(code or "").split())
    if not text.startswith(INVITE_PREFIX):
        raise ConnectError("That is not an invite code. It should start with " + INVITE_PREFIX)
    body = text[len(INVITE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        packed, check = raw[:-3], raw[-3:]
        if hashlib.sha256(packed).digest()[:3] != check:
            raise ValueError("check")
        data = json.loads(zlib.decompress(packed, 0, 65536))
    except (ValueError, zlib.error, TypeError):
        raise ConnectError("That invite code is damaged or was cut short. Copy it again.") from None
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ConnectError("That invite is from a newer PrivatePartyNet. Update to use it.")
    candidates = []
    for item in data.get("c") or []:
        try:
            kind, address, port = item
            ipaddress.ip_address(address)
            if kind in ("lan", "ipv6", "public") and 0 < int(port) < 65536:
                candidates.append([kind, address, int(port)])
        except (ValueError, TypeError):
            continue
    fingerprint = data.get("f")
    if not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise ConnectError("That invite does not carry the host's certificate, so it cannot be checked.")
    secret = data.get("r") if isinstance(data.get("r"), str) and re.fullmatch(r"[0-9a-f]{32}", data.get("r") or "") else ""
    host_sign = data.get("k") if isinstance(data.get("k"), str) and re.fullmatch(r"[0-9a-f]{64}", data.get("k") or "") else ""
    return {"name": str(data.get("n") or "Party")[:40], "password": str(data.get("p") or ""), "candidates": candidates,
            "fingerprint": fingerprint, "server_id": str(data.get("s") or "")[:40], "host_sign": host_sign,
            "secret": secret, "relays": _read_relays(data.get("y"))}


def _read_relays(items):
    """The relays in an invite, as punch.join_relayed takes them; anything malformed is left out."""
    found = []
    for item in (items if isinstance(items, list) else [])[:MAX_RELAYS]:
        try:
            kind, host, port, tls_port, pin, token = item
            token = bytes.fromhex(token)
            host, port, tls_port, pin = str(host), int(port), int(tls_port or 0), str(pin or "")
        except (ValueError, TypeError):
            continue
        if kind not in ("vps", "friend") or not host or len(host) > 253 or len(token) != {"vps": 52, "friend": 116}.get(kind):
            continue
        if not 0 < port < 65536 or not 0 <= tls_port < 65536 or (pin and not re.fullmatch(r"[0-9a-f]{64}", pin)):
            continue
        if kind == "friend":
            try:
                ipaddress.ip_address(host)  # a member's PC is named by its address, never by a name to look up
            except ValueError:
                continue
        found.append({"kind": kind, "host": host, "port": port, "tls_port": tls_port, "pin": pin, "token": token})
    return found


# --- asking to join ---------------------------------------------------------------------------------------------


def _order(candidates, my_lan=None):
    """Try the host's addresses on my own network first, then IPv6, then the public one."""
    my_lan = my_lan if my_lan is not None else [a for a in lan_addresses() if a != LOOPBACK]
    prefixes = {a.rsplit(".", 1)[0] for a in my_lan}

    def rank(item):
        kind, address, _ = item
        if kind == "lan":
            return 0 if address.rsplit(".", 1)[0] in prefixes else 3
        return {"ipv6": 1, "public": 2}.get(kind, 4)

    return sorted(candidates, key=rank)


def _connect(address, port, fingerprint, timeout):
    """An HTTPS connection to the host whose certificate matches the pinned fingerprint, or ConnectError."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint check just below
    connection = http.client.HTTPSConnection(address, port, timeout=timeout, context=context)
    connection.connect()
    der = connection.sock.getpeercert(binary_form=True)
    seen = hashlib.sha256(der).hexdigest() if der else ""
    if fingerprint and seen != fingerprint:
        connection.close()
        raise ConnectError("The party at that address is not the one this invite is for (its certificate differs).")
    connection.seen_fingerprint = seen  # with no fingerprint to check against, the first one seen is what gets remembered
    return connection


def _base(address, port):
    return f"https://[{address}]:{port}" if ":" in address else f"https://{address}:{port}"


class Joiner:
    """One attempt to join a party from an invite. Run `run()` in a thread and read `status` from anywhere.

    status["state"] moves through: trying, waiting (the owner or an admin has been asked), allowed, denied, failed, gone.
    When allowed, status["url"] is the join link to open."""

    def __init__(self, invite, name, identity=None, my_lan=None, answer_wait=ANSWER_WAIT, poll=POLL_SECONDS,
                 timeout=ASK_TIMEOUT):
        """`identity` (an identity.Identity) signs the request, so the server knows it is really you: someone it has
        let in before comes straight back in, and keeps their role. Without one you are a stranger every time."""
        self.invite = invite
        self.name = name
        self.identity = identity
        self._my_lan = my_lan
        self._answer_wait = answer_wait
        self._poll = poll
        self._timeout = timeout
        self._cancel = threading.Event()
        self.seen_fingerprint = ""
        self.tunnel = None  # the punch-through tunnel, when that is how the party was reached (kept open by the app)
        self.status = {"state": "trying", "message": "Looking for the party...", "url": ""}

    def cancel(self):
        self._cancel.set()

    def _set(self, state, message, url=""):
        self.status = {"state": state, "message": message, "url": url}

    def run(self):
        invite = self.invite
        tried = []
        for kind, address, port in _order(invite["candidates"], self._my_lan):
            if self._cancel.is_set():
                self._set("failed", "Cancelled.")
                return
            self._set("trying", f"Trying {address}...")
            try:
                ticket = self._ask(address, port)
            except ConnectError as exc:
                tried.append(str(exc))
                if "certificate differs" in str(exc) or "banned" in str(exc) or "wrong passwords" in str(exc):
                    self._set("failed", str(exc))
                    return
                continue
            except OSError:
                tried.append(f"{address} did not answer")
                continue
            if ticket is None:
                self._set("failed", "That password is not right." if self.invite["password"] else
                          "This party needs its password. Ask the host for an invite.")
                return
            self._wait(address, port, ticket)
            return
        if self._punch(tried):
            return
        why = "; ".join(sorted(set(tried))[:3]) or "no address to try"
        self._set("failed", "Could not reach the party (" + why + ").")

    def _punch(self, tried):
        """Reach the host through both routers (punch.py) when no address answered. True when it was tried to the end
        (joined, denied or failed with its own message); False when the invite cannot do it."""
        invite = self.invite
        relays = invite.get("relays") or []
        findable = invite.get("host_sign") and invite.get("server_id")
        if not (invite.get("secret") and invite["fingerprint"] and (findable or relays)):
            return False
        if self._cancel.is_set():
            return False
        from privatepartynet import punch

        self._set("trying", "No address answered; trying a direct connection through both routers, then a relay "
                            "(this takes up to a minute)..." if relays else
                            "No address answered; trying a direct connection through both routers (this takes up to a minute)...")
        try:
            tunnel = punch.join_through(invite.get("host_sign", ""), invite.get("server_id", ""),
                                        bytes.fromhex(invite["secret"]), invite["fingerprint"], relays=relays,
                                        ipv6=global_ipv6_addresses)
        except Exception as exc:
            why = "; ".join(sorted(set(tried))[:2])
            self._set("failed", f"Could not reach the party ({why}). The connection through the routers did not work "
                      f"either: {exc}")
            return True
        self.tunnel = tunnel
        try:
            ticket = self._ask("127.0.0.1", tunnel.port)
        except (ConnectError, OSError) as exc:
            tunnel.close()
            self.tunnel = None
            self._set("failed", str(exc))
            return True
        if ticket is None:
            tunnel.close()
            self.tunnel = None
            self._set("failed", "That password is not right.")
            return True
        self._wait("127.0.0.1", tunnel.port, ticket)
        if self.status["state"] != "allowed":
            tunnel.close()  # not let in: nothing will use it
            self.tunnel = None
        return True

    def _ask(self, address, port):
        fingerprint = self.invite["fingerprint"]
        body = {"name": self.name, "password": self.invite["password"]}
        if self.identity is not None:
            connection = _connect(address, port, fingerprint, self._timeout)
            try:
                connection.request("GET", "/ask?challenge=1")
                response = connection.getresponse()
                challenge = json.loads(response.read())
                seen = connection.seen_fingerprint
            finally:
                connection.close()
            if response.status != 200 or not isinstance(challenge.get("nonce"), str):
                raise ConnectError("The party did not answer the sign-in step.")
            if not fingerprint:
                # Joining by address: the first certificate seen is the one trusted from now on, for every later
                # connection of this attempt and for the remembered server.
                fingerprint = self.invite["fingerprint"] = seen
            # Sign what this side checked itself: the certificate it saw, not anything the server said about itself.
            message = ask_message(seen, challenge["nonce"], self._clean_name())
            body.update(card=self.identity.card(self.name), nonce=challenge["nonce"],
                        sig=self.identity.sign(message).hex())
        connection = _connect(address, port, fingerprint, self._timeout)
        self.seen_fingerprint = connection.seen_fingerprint
        if not self.invite["fingerprint"]:
            self.invite["fingerprint"] = self.seen_fingerprint
        try:
            connection.request("POST", "/ask", body=json.dumps(body), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        if response.status == 403:
            text = data.decode("utf-8", "replace")
            if "password" in text:
                return None
            raise ConnectError(text or "The party refused the request.")
        if response.status == 409:
            raise ConnectError(data.decode("utf-8", "replace") or "The party has too many people waiting; try again in a minute.")
        if response.status != 200:
            raise ConnectError(f"The party answered {response.status}.")
        return json.loads(data)["ticket"]

    def _clean_name(self):
        """The name exactly as the server will clean it, since the signature covers it."""
        from privatepartynet.discovery import clean_name

        return clean_name(self.name or "Guest")

    def _wait(self, address, port, ticket):
        self._set("waiting", "Waiting for the owner or an admin to let you in...")
        deadline = time.monotonic() + self._answer_wait
        while time.monotonic() < deadline and not self._cancel.is_set():
            try:
                connection = _connect(address, port, self.invite["fingerprint"], self._timeout)
                try:
                    connection.request("GET", "/ask?ticket=" + urllib.parse.quote(ticket))
                    answer = json.loads(connection.getresponse().read())
                finally:
                    connection.close()
            except (OSError, ConnectError, ValueError):
                answer = {"state": "pending"}
            state = answer.get("state")
            if state == "allowed":
                self._set("allowed", "You are in.", f"{_base(address, port)}/join?token={answer['token']}")
                return
            if state == "denied":
                self._set("denied", "The owner said no.")
                return
            if state == "gone":
                self._set("gone", "The request expired. Ask again.")
                return
            self._cancel.wait(self._poll)
        self._set("failed", "Cancelled." if self._cancel.is_set() else "Nobody answered in time.")
