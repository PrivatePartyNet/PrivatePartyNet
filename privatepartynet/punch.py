"""Reaching a party whose host lets nothing in (carrier-grade NAT, no IPv6, no open port).

Both PCs make one UDP socket each, learn the public address their router gives that socket (STUN), and swap those
addresses through signed records on the BitTorrent DHT (dht.py): the host publishes its own under its identity key, and a
joiner writes its own into a "mailbox" whose key both sides derive from a secret in the invite. Then both send small
probes to each other's public address at the same time, which opens each router for the other's packets (hole punching).
Over that same socket a QUIC connection (aioquic) carries the party's own TLS connections, one QUIC stream each, so the
pages, the join and the game network work exactly as on a LAN. The joiner sees them on a loopback port.

Punching cannot cross two routers that each give a new public port for every destination ("hard" NAT on both sides).
Then, or when the host's address is not found at all, the same QUIC connection goes through a relay instead (see
relayproto.py): the group's own relay server or a member who relays for others, both listed in the invite with a
token for that invite. Joiner and host meet there on a channel only the invite's holders can work out, and the relay
carries QUIC's ciphertext without being able to read it. The host keeps the channels of its live invites bound on every
relay it knows of, so it needs no message to start. With no relay in the invite the app says plainly that this case
cannot work.

IPv6 comes first where both PCs have it. It has no NAT, only firewalls that let in answers to what the PC sent, so
the same sending at once opens them, whatever the IPv4 routers are like (most carrier-grade NAT providers give real
IPv6). The host's record and the joiner's mailbox carry an IPv6 address and port beside the IPv4 ones, and the host
keeps a second QUIC socket on IPv6.

The host hands each tunnelled connection to the party from its own address in 127.1.0.0/16 (one per joiner), which the
party treats as outside the network: such a joiner needs the password and is rate limited on their own.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
import struct
import threading
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from privatepartynet import relayproto
from privatepartynet.connect import STUN_SERVERS, _stun_binding

log = logging.getLogger("privatepartynet.punch")

ALPN = "privatepartynet-tunnel/1"
PROBE = b"PRIVATEPARTYNET-PUNCH/1"
PROBE_SECONDS = 0.2
HOST_SALT = b"privatepartynet-host"
GUEST_SALT = b"privatepartynet-guest"
TUNNEL_NETWORK = ipaddress.ip_network("127.1.0.0/16")  # where tunnelled connections come from, as the party sees them
CONNECT_SECONDS = 30.0  # through a relay, where the host is waiting already
# How long a joiner keeps its side open for the host to notice it. The host learns of a joiner only by reading the
# invite's mailbox on the DHT: one lookup takes up to 20 seconds (measured), every WATCH_SECONDS. 3.1 waited 30 seconds
# (8 on IPv6), while a 3.1 host read its mailboxes one after another with 15 seconds between rounds, so the joiner had
# usually given up before the host opened its side.
PUNCH_SECONDS = 100.0
RELAY_PUNCH_SECONDS = 45.0  # the same when a relay is there to fall back on
QUICK_SECONDS = 6.0  # how long to try punching from behind a hard router before taking a relay instead
ATTEMPT_SECONDS = 10.0  # a fresh QUIC handshake this often, so its resends never back off past the host's first probe
WATCH_SECONDS = 5.0  # how often the host reads the invites' mailboxes (all at once)
IDLE_SECONDS = 60.0


class PunchError(Exception):
    """Why a way through did not work, in words, with `facts` about what was found on the way (for the message the
    person sees): host_found, host_version, host_v6, host_nat, my_v6, my_nat, mailbox_stored."""

    def __init__(self, message="", facts=None):
        super().__init__(message)
        self.facts = dict(facts or {})


class Unanswered(PunchError):
    """The host never finished a handshake. `heard` says whether its probes arrived (it had opened its side)."""

    def __init__(self, message, heard=False):
        super().__init__(message)
        self.heard = heard


def _nat_words(nat):
    """A router kind from STUN, in words for the log."""
    return {"easy": "keeps one port per socket (punching can work)",
            "hard": "gives a new port for every destination (strict, as carrier-grade NAT often is)",
            }.get(nat, "of an unknown kind (fewer than two STUN servers answered)")


# --- the rendezvous records -----------------------------------------------------------------------------------------


def mailbox_key(secret):
    """The key both sides derive from the invite's secret: whoever holds the invite may write their address there."""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"PrivatePartyNet mailbox|" + bytes(secret)).digest())


def host_salt(server_id):
    return HOST_SALT + b"|" + str(server_id).encode("ascii", "replace")[:40]


def record(address, port, nat, v6=None):
    """A DHT record: the IPv4 address and port STUN saw (address None when there is none), and `v6`, an IPv6
    (address, port), when there is one."""
    from privatepartynet import __version__

    value = {"t": int(time.time()), "w": __version__}  # "w": which PrivatePartyNet wrote it (3.2 on; older ones have none)
    if address is not None:
        value.update(a=address, p=int(port), n=nat)
    if v6 is not None:
        value.update({"6": v6[0], "q": int(v6[1])})
    return value


def read_record6(value, max_age=600):
    """(address, port) of the IPv6 side of a record, or None when it has none or is malformed or stale."""
    if not isinstance(value, dict):
        return None
    try:
        address = ipaddress.IPv6Address(value[b"6"].decode("ascii"))
        port, stamp = int(value[b"q"]), int(value[b"t"])
    except (KeyError, ValueError, AttributeError, UnicodeDecodeError):
        return None
    if not 0 < port < 65536 or abs(time.time() - stamp) > max_age:
        return None
    return str(address), port


def _full(address):
    """An address as an IPv6 socket wants it, with flow and scope (Windows refuses the two-part form); IPv4 as it is."""
    return (address[0], int(address[1]), 0, 0) if ":" in address[0] else (address[0], int(address[1]))


def _v6_socket():
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(("::", 0))
    except OSError:
        sock.close()
        raise
    return sock


def read_record(value, max_age=600):
    """(address, port, nat) from a record, or None when it is malformed or older than `max_age` seconds."""
    if not isinstance(value, dict):
        return None
    try:
        address = ipaddress.ip_address(value[b"a"].decode("ascii"))
        port, stamp = int(value[b"p"]), int(value[b"t"])
        nat = value.get(b"n", b"unknown").decode("ascii", "replace")
    except (KeyError, ValueError, AttributeError, UnicodeDecodeError):
        return None
    if not 0 < port < 65536 or abs(time.time() - stamp) > max_age:
        return None
    return str(address), port, nat


def record_version(value):
    """The PrivatePartyNet version that wrote a record, or "" for 3.1 and older, which did not say."""
    try:
        text = value[b"w"].decode("ascii")
    except (KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return ""
    return text if len(text) <= 20 and all(ch.isalnum() or ch in ".-+" for ch in text) else ""


def join_channel(secret):
    """The relay channel for an invite: the joiner and the host both work it out from the invite's secret."""
    return relayproto.channel_for("join", bytes(secret))


def tunnel_source(remote):
    """The loopback address a joiner's tunnelled connections come from: one per public address, in 127.1.0.0/16."""
    digest = hashlib.sha256(f"{remote[0]}:{remote[1]}".encode("ascii")).digest()
    return f"127.1.{digest[0]}.{max(1, digest[1])}"


# --- STUN on a given socket -----------------------------------------------------------------------------------------


def learn_mapping(sock, servers=STUN_SERVERS, timeout=2.0):
    """(public address, public port, nat) for this socket, asked of several STUN servers from it. Raises PunchError when
    no server answers. nat is "easy" when every server saw the same port, "hard" when they differ."""
    previous = sock.gettimeout()
    sock.settimeout(timeout)
    seen = []
    try:
        for host, port in servers:
            try:
                found = _stun_binding(sock, host, port, socket.AF_INET)
            except (OSError, struct.error):
                continue
            if found:
                seen.append(found)
    finally:
        sock.settimeout(previous)
    if not seen:
        raise PunchError("no STUN server answered, so this PC's public address is not known")
    ports = {port for _, port in seen}
    nat = "unknown" if len(seen) < 2 else ("easy" if len(ports) == 1 else "hard")
    return seen[0][0], seen[0][1], nat


# --- an asyncio loop on its own thread ------------------------------------------------------------------------------


class _Loop:
    def __init__(self, name):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self.thread.start()

    def run(self, coroutine, timeout):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def stop(self, closing=()):
        """Close `closing` (transports, servers) inside the loop, let cancelled tasks finish, then stop the loop."""
        def finish():
            for item in closing:
                try:
                    item.close()
                except Exception:
                    pass
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            self.loop.call_later(0.2, self.loop.stop)  # give the cancelled tasks a turn to end

        if self.loop.is_running():
            self.loop.call_soon_threadsafe(finish)
        self.thread.join(timeout=5)
        if not self.loop.is_running():
            self.loop.close()


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError, RuntimeError, ValueError):
            pass  # ValueError: aioquic's way of saying the stream had already finished both ways


class _Prober(asyncio.DatagramProtocol):
    """Sits beside QUIC on the same socket only long enough to send probes; never used once QUIC owns the socket."""


class _Switch(asyncio.DatagramProtocol):
    """A joiner's socket: QUIC's packets go to the handshake in progress (`inner`), and the host's probes are noted
    (`heard`), since the first one means the host has just opened its side."""

    def __init__(self, remote):
        self.inner = None
        self.remote = remote
        self.heard = asyncio.Event()
        self.answered = False  # something other than a probe came back for the handshake in progress

    def datagram_received(self, data, addr):
        if _same_address(addr, self.remote):
            # QUIC compares addresses as text, and one address has several spellings (Windows writes ::ffff:127.0.0.1
            # where Python writes ::ffff:7f00:1): an answer from the host must look like it came from where QUIC sent.
            addr = self.remote
        if data == PROBE:
            self.heard.set()
            return
        self.answered = True
        if self.inner is not None:
            self.inner.datagram_received(data, addr)

    def error_received(self, exc):
        pass  # on Windows, an ICMP "port unreachable" for an earlier packet; the host may not be listening yet

    def connection_lost(self, exc):
        if self.inner is not None:
            self.inner.connection_lost(exc)


def _peer_of(writer):
    """The joiner's public (address, port) for a QUIC stream's writer. aioquic's stream writers give no "peername",
    so before 3.2 every tunnelled joiner looked like 0.0.0.0 port 0 and all shared one source address in the party."""
    try:
        paths = writer.transport.protocol._quic._network_paths
        return tuple(paths[0].addr[:2])
    except (AttributeError, IndexError, TypeError):
        return tuple((writer.get_extra_info("peername") or ("0.0.0.0", 0))[:2])


def _same_address(one, two):
    try:
        return ipaddress.ip_address(one[0].split("%")[0]) == ipaddress.ip_address(two[0].split("%")[0]) and             int(one[1]) == int(two[1])
    except (ValueError, TypeError, IndexError):
        return False


def _abandon(protocol):
    """Drop a QUIC handshake that went unanswered, without a word on the wire: its timer stops and it sends nothing."""
    if protocol._timer is not None:
        protocol._timer.cancel()
        protocol._timer = None
    protocol.transmit = lambda: None


# --- the host's side ------------------------------------------------------------------------------------------------


class TunnelHost:
    """QUIC on one punched socket, handing each stream to the party as a TCP connection to `party_port`."""

    def __init__(self, sock, certfile, keyfile, party_port):
        from aioquic.asyncio.server import QuicServer
        from aioquic.quic.configuration import QuicConfiguration

        self.sock = sock
        self.party_port = party_port
        self._loop = _Loop("privatepartynet-tunnel-host")
        configuration = QuicConfiguration(is_client=False, alpn_protocols=[ALPN], idle_timeout=IDLE_SECONDS)
        configuration.load_cert_chain(certfile, keyfile)
        self.peers = set()

        def handler(reader, writer):
            asyncio.ensure_future(self._serve_stream(reader, writer))

        async def start():
            transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: QuicServer(configuration=configuration, stream_handler=handler), sock=sock)
            return transport, protocol

        self.transport, self.server = self._loop.run(start(), 10)

    async def _serve_stream(self, reader, writer):
        remote = _peer_of(writer)
        if remote[:2] not in self.peers:
            way = "IPv6" if ":" in str(remote[0]) else "a relay" if str(remote[0]).startswith("127.") else "IPv4"
            log.info("A joiner's tunnel to the party opened, over %s, from %s port %s", way, remote[0], remote[1])
        self.peers.add(remote[:2])
        try:
            party_reader, party_writer = await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", self.party_port, local_addr=(tunnel_source(remote[:2]), 0)), 10)
        except (OSError, asyncio.TimeoutError):
            writer.close()
            return
        await asyncio.gather(_pipe(reader, party_writer), _pipe(party_reader, writer))

    def probe(self, remote, seconds=3.0):
        """Send probes to a joiner's public address from the QUIC socket, opening this router for their packets."""
        async def send():
            ends = time.monotonic() + seconds
            while time.monotonic() < ends:
                self.transport.sendto(PROBE, _full(remote))
                await asyncio.sleep(PROBE_SECONDS)

        asyncio.run_coroutine_threadsafe(send(), self._loop.loop)

    def close(self):
        self._loop.stop(closing=(self.server, self.transport))


# --- the joiner's side ----------------------------------------------------------------------------------------------


class TunnelClient:
    """QUIC from one punched socket to the host, pinned to the host certificate's fingerprint. Connections to the
    loopback port `port` are carried to the party, one QUIC stream each."""

    def __init__(self, sock, remote, fingerprint, connect_seconds=CONNECT_SECONDS, bridge=None):
        from aioquic.asyncio.protocol import QuicConnectionProtocol
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection

        self.sock = sock
        self.remote = _full((remote[0], int(remote[1])))
        self.bridge = bridge  # the relay this tunnel goes through, if it does; it closes with the tunnel
        self.kind = "relay" if bridge is not None else "punch"
        self._loop = _Loop("privatepartynet-tunnel-join")
        self.heard_host = False  # whether the host's probes arrived: it had opened its side
        # The idle limit must outlast the wait for the host to open its router, or QUIC gives up first (seen: 60 s).
        configuration = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                          idle_timeout=max(IDLE_SECONDS, connect_seconds + 15),
                                          verify_mode=ssl.CERT_NONE, server_name="privatepartynet")

        async def start():
            loop = asyncio.get_running_loop()
            switch = _Switch(self.remote)
            transport, _ = await loop.create_datagram_endpoint(lambda: switch, sock=sock)
            # Keep sending probes until the handshake is through: they open this router for the host's answers.
            probing = True

            async def probe():
                while probing:
                    transport.sendto(PROBE, self.remote)
                    await asyncio.sleep(PROBE_SECONDS)

            prober = asyncio.ensure_future(probe())
            deadline = loop.time() + connect_seconds
            protocol = None
            try:
                while True:
                    # A fresh handshake every ATTEMPT_SECONDS, and at once when the host's first probe shows it has
                    # opened its side: QUIC resends its first packet less and less often (0.2 s, 0.4 s, ... 25 s), so
                    # a single handshake could still be waiting to resend long after the host became reachable.
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    if protocol is not None:
                        _abandon(protocol)
                    protocol = switch.inner = QuicConnectionProtocol(QuicConnection(configuration=configuration))
                    protocol.connection_made(transport)
                    switch.answered = False
                    protocol.connect(self.remote)
                    connected = asyncio.ensure_future(protocol.wait_connected())
                    waits = {connected}
                    heard = None
                    if not switch.heard.is_set():
                        heard = asyncio.ensure_future(switch.heard.wait())
                        waits.add(heard)
                    done, _ = await asyncio.wait(waits, timeout=min(remaining, ATTEMPT_SECONDS),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if heard is not None and heard not in done:
                        heard.cancel()
                    if connected not in done and switch.answered:
                        # The host answered this handshake already (it was reachable before its probe came): let it
                        # finish rather than start again.
                        done, _ = await asyncio.wait({connected}, timeout=min(ATTEMPT_SECONDS,
                                                                              max(0.1, deadline - loop.time())))
                    if connected in done:
                        connected.result()
                        break
                    connected.cancel()
            finally:
                probing = False
                prober.cancel()
                self.heard_host = switch.heard.is_set()
            certificate = protocol._quic.tls._peer_certificate
            from cryptography.hazmat.primitives.serialization import Encoding

            seen = hashlib.sha256(certificate.public_bytes(Encoding.DER)).hexdigest() if certificate else ""
            if seen != fingerprint:
                protocol.close()
                raise PunchError("the host at that address is not the one this invite is for (its certificate differs)")

            async def carry(local_reader, local_writer):
                reader, writer = await protocol.create_stream()
                # aioquic picks the next stream id not yet used, and a stream is used only once something is sent on
                # it: claim it now, or a connection opened before this one sends would get the same stream (seen as
                # TLS errors when several connections open at once).
                writer.write(b"")
                await asyncio.gather(_pipe(local_reader, writer), _pipe(reader, local_writer))

            listener = await asyncio.start_server(carry, "127.0.0.1", 0)
            return transport, protocol, listener

        try:
            self.transport, self.protocol, self._listener = self._loop.run(start(), connect_seconds + 5)
        except Exception as exc:
            self._loop.stop()
            try:
                sock.close()  # the transport owned it, but never closed it on the way out
            except OSError:
                pass
            if isinstance(exc, TimeoutError):
                raise Unanswered(f"no answer within {connect_seconds:.0f} seconds", self.heard_host) from exc
            raise
        self.port = self._listener.sockets[0].getsockname()[1]

    def close(self):
        self._loop.stop(closing=(self._listener, self.protocol, self.transport))
        if self.bridge is not None:
            self.bridge.close()


# --- the whole rendezvous -------------------------------------------------------------------------------------------


class HostPunch:
    """While a party is open: keep a punched socket with a known public address, publish it on the DHT, watch the
    mailboxes of the invites given out, and open the way for each joiner who writes there."""

    def __init__(self, identity_key, server_id, certfile, keyfile, party_port, secrets_, dht_factory=None,
                 stun_servers=STUN_SERVERS, refresh=60.0, poll=WATCH_SECONDS, max_seconds=24 * 3600, relays=None,
                 ipv6=None):
        """`relays` is a callable giving the relays to wait on (Party.host_relays): with them the host stays reachable
        even when no STUN server answers, as on a network that lets no UDP out (the relays then fall back to TLS).
        `ipv6` is a callable giving this PC's global IPv6 addresses, best first (connect.global_ipv6_addresses);
        with one, joiners are let in over IPv6 too."""
        self._key = identity_key
        self._server_id = server_id
        self._secrets = secrets_  # a callable: the invite secrets still valid
        self._dht_factory = dht_factory
        self._stun = stun_servers
        self._refresh, self._poll, self._max_seconds = refresh, poll, max_seconds
        self._stop = threading.Event()
        self._relays = relays or (lambda: [])
        self.bridges = {}  # (host, port) of a relay -> RelayBridge carrying the invites' channels to the QUIC socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock6, self.tunnel6, self.mapping6 = None, None, None
        addresses = list(ipv6()) if ipv6 else []
        if addresses:
            try:
                self.sock6 = _v6_socket()
                self.mapping6 = (addresses[0], self.sock6.getsockname()[1])
            except OSError:
                self.sock6 = None
        try:
            self.mapping = learn_mapping(self.sock, stun_servers)
        except PunchError:
            if not self._relays() and self.mapping6 is None:
                self.sock.close()
                if self.sock6 is not None:
                    self.sock6.close()
                raise
            self.mapping = None  # nobody can punch through to us on IPv4, but IPv6 or the relays still bring joiners in
        self.tunnel = TunnelHost(self.sock, certfile, keyfile, party_port)
        if self.sock6 is not None:
            self.tunnel6 = TunnelHost(self.sock6, certfile, keyfile, party_port)
        self.seen = {}  # joiner's public address -> time it was last opened for
        self._published = None  # how many DHT nodes took the last record, for the log
        if self.mapping is not None:
            log.info("Ready to be reached through the routers. IPv4: %s port %s, router %s", self.mapping[0],
                     self.mapping[1], _nat_words(self.mapping[2]))
        else:
            log.info("Ready to be reached through the routers, but not on IPv4: no STUN server answered")
        log.info("IPv6: %s", f"{self.mapping6[0]} port {self.mapping6[1]}" if self.mapping6 else
                 "none (this PC has no global IPv6 address, so joiners cannot use IPv6)")
        log.info("Relays to meet joiners on: %d", len(self._relays()))
        self._sync_relays()
        self._thread = threading.Thread(target=self._run, name="privatepartynet-punch-host", daemon=True)
        self._thread.start()

    def _dht(self):
        from privatepartynet.dht import Dht

        return self._dht_factory() if self._dht_factory else Dht()

    def _run(self):
        """Two jobs at once, each on its own DHT client, because one DHT lookup takes about 20 seconds (measured): keep
        the host record fresh, and keep reading the invites' mailboxes so a joiner is seen within a lookup or two."""
        watcher = threading.Thread(target=self._watch, name="privatepartynet-punch-watch", daemon=True)
        watcher.start()
        started = time.monotonic()
        seq = int(time.time())
        dht = self._dht()
        try:
            while not self._stop.is_set() and time.monotonic() - started < self._max_seconds:
                seq = max(seq + 1, int(time.time()))
                if self.mapping is not None or self.mapping6 is not None:
                    address, port, nat = self.mapping or (None, 0, "")
                    try:
                        stored = dht.put_mutable(self._key, record(address, port, nat, self.mapping6), seq,
                                                 host_salt(self._server_id))
                    except Exception:
                        log.debug("Publishing the host record failed", exc_info=True)
                        stored = 0
                    if self._published is None or (stored == 0) != (self._published == 0):
                        log.info("This party's address on the DHT: stored on %d nodes%s", stored,
                                 "" if stored else "; joiners cannot find it until it is")
                    self._published = stored
                self._stop.wait(self._refresh)
        finally:
            dht.close()
            watcher.join(timeout=30)

    def _watch(self):
        """Read every live invite's mailbox at once, every `poll` seconds. One DHT lookup takes up to 20 seconds, so
        reading them one after another (as 3.1 did, with up to 20 invites) kept a joiner waiting for minutes."""
        started = time.monotonic()
        dht = self._dht()
        seqs = {}  # mailbox -> the newest seq acted on
        try:
            while not self._stop.is_set() and time.monotonic() - started < self._max_seconds:
                self._sync_relays()
                found = {}

                def read(secret):
                    try:
                        found[secret] = dht.get_mutable(mailbox_key(secret).public_key().public_bytes_raw(), GUEST_SALT)
                    except Exception:
                        log.debug("Reading an invite's mailbox failed", exc_info=True)

                readers = [threading.Thread(target=read, args=(secret,), name="privatepartynet-punch-mailbox",
                                            daemon=True) for secret in list(self._secrets())]
                for reader in readers:
                    reader.start()
                for reader in readers:
                    reader.join(timeout=60)
                for secret, entry in list(found.items()):
                    if self._stop.is_set():
                        break
                    mailbox = mailbox_key(secret).public_key().public_bytes_raw()
                    if not entry or entry[0] <= seqs.get(mailbox, 0):
                        continue
                    seqs[mailbox] = entry[0]
                    self._open_for(secret, entry[1])
                self._stop.wait(self._poll)
        finally:
            dht.close()

    def _open_for(self, secret, value):
        """A joiner wrote to an invite's mailbox: send probes to each address of theirs, which opens this PC's router
        (and its IPv6 firewall) for their packets."""
        joiner, joiner6 = read_record(value), read_record6(value)
        log.info("A joiner wrote to the mailbox of invite %s (PrivatePartyNet %s). IPv4: %s. IPv6: %s",
                 hashlib.sha256(bytes(secret)).hexdigest()[:8], record_version(value) or "3.1 or older",
                 f"{joiner[0]} port {joiner[1]}, router {_nat_words(joiner[2])}" if joiner else "none",
                 f"{joiner6[0]} port {joiner6[1]}" if joiner6 else "none")
        if joiner:
            self.seen[joiner[:2]] = time.monotonic()
            self.tunnel.probe(joiner[:2], seconds=PUNCH_SECONDS)
        if joiner6 and self.tunnel6 is not None:
            # Opens this PC's IPv6 firewall for the joiner, as the probes on IPv4 open its router.
            self.seen[joiner6] = time.monotonic()
            self.tunnel6.probe(joiner6, seconds=PUNCH_SECONDS)
        elif joiner6:
            log.info("The joiner has IPv6 but this PC has none, so it cannot be used")
        if joiner and self.mapping is not None and joiner[2] == "hard" and self.mapping[2] == "hard":
            log.info("Both routers give a new port for every destination, so IPv4 cannot get through; %s",
                     "IPv6 may" if joiner6 and self.tunnel6 is not None else
                     "only a relay can join them" + ("" if self._relays() else ", and this party has none"))

    def _sync_relays(self):
        """Keep every live invite's channel bound on every relay there is now, each carried to the QUIC socket."""
        from privatepartynet.relayclient import RelayBridge

        try:
            relays = {(r["host"], int(r["port"])): r for r in self._relays()}
            channels = {join_channel(secret) for secret in self._secrets()}
        except Exception:
            log.debug("Reading the relays failed", exc_info=True)
            return
        target = ("127.0.0.1", self.sock.getsockname()[1])
        for key in [k for k in self.bridges if k not in relays]:
            self.bridges.pop(key).close()
        for key, relay in relays.items():
            bridge = self.bridges.get(key)
            if bridge is None:
                bridge = self.bridges[key] = RelayBridge(relay)
            bridge.client.relay["credential"] = relay["credential"]
            for channel in list(bridge.client.channels):
                if channel not in channels:
                    bridge.remove(channel)
            for channel in channels:
                bridge.add(channel, credential=relay["credential"], target=target)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=30)
        for bridge in list(self.bridges.values()):
            bridge.close()
        self.bridges.clear()
        self.tunnel.close()
        if self.tunnel6 is not None:
            self.tunnel6.close()
        if self.sock6 is not None:
            try:
                self.sock6.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


BOTH_HARD = ("both routers give a new port for every connection (strict carrier-grade NAT), so a direct way cannot "
             "open. Set up a relay server (relay/README.md) or have a member turn on friend relay")


def join_through(host_key, server_id, secret, fingerprint, dht_factory=None, stun_servers=STUN_SERVERS,
                 connect_seconds=None, find_seconds=60.0, relays=(), ipv6=None):
    """From a joiner's PC: find the host's public address on the DHT, write ours into the invite's mailbox, punch, and
    open the QUIC tunnel; when that cannot work, open it through one of `relays` (from the invite) instead. Returns a
    TunnelClient whose .port is the loopback port to reach the party on, and whose .kind says which way it went
    ("ipv6", "punch" or "relay"). `ipv6` is a callable giving this PC's global IPv6 addresses; with one, and one in the
    host's record, IPv6 is tried beside IPv4. `connect_seconds` is how long to wait for the host to open its side
    (PUNCH_SECONDS, or RELAY_PUNCH_SECONDS with a relay to fall back on). Raises PunchError, in words, with .facts."""
    if not (host_key and server_id):
        if not relays:
            raise PunchError("the invite has nothing to find the host by")
        return join_relayed(secret, fingerprint, relays, connect_seconds or CONNECT_SECONDS)
    seconds = connect_seconds or (RELAY_PUNCH_SECONDS if relays else PUNCH_SECONDS)
    try:
        return _join_punched(host_key, server_id, secret, fingerprint, dht_factory, stun_servers, seconds,
                             find_seconds, quick=bool(relays), ipv6=ipv6)
    except PunchError as exc:
        if not relays or "certificate differs" in str(exc):
            raise
        log.info("Punching through to the host did not work (%s); trying the %d relays in the invite", exc, len(relays))
        try:
            return join_relayed(secret, fingerprint, relays, connect_seconds or CONNECT_SECONDS)
        except PunchError as relayed:
            raise PunchError(f"{exc}; and through the relays: {relayed}", {**exc.facts, "relays_tried": True}) from None


def join_relayed(secret, fingerprint, relays, connect_seconds=CONNECT_SECONDS, bind_seconds=8.0):
    """The QUIC tunnel through the first relay that works, in the order given. Each relay is as an invite carries it
    (connect.decode_invite): {"kind", "host", "port", "tls_port", "pin", "token": bytes}. The host's certificate is
    checked exactly as on a direct path."""
    from privatepartynet.pathway import PassCredential, invite_key
    from privatepartynet.relayclient import RelayBridge

    channel = join_channel(secret)
    why = []
    for relay in relays:
        what = "the relay server" if relay.get("kind") == "vps" else "a member who relays"
        try:
            if relay.get("kind") == "friend":
                credential = PassCredential(relay["token"], invite_key(secret)[0])
            else:
                credential = relayproto.HmacCredential(relay["token"])
        except (KeyError, ValueError):
            why.append(f"{relay.get('host')}: the invite's credentials for it are damaged")
            log.info("Relay %s (%s): the invite's credentials for it are damaged", relay.get("host"), what)
            continue
        log.info("Trying %s at %s port %s", what, relay["host"], relay["port"])
        bridge = RelayBridge({**relay, "credential": credential})
        try:
            port = bridge.add(channel)
            if not bridge.wait_bound(channel, bind_seconds):
                raise PunchError(f"{relay['host']} did not answer")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            try:
                client = TunnelClient(sock, ("127.0.0.1", port), fingerprint, connect_seconds=connect_seconds,
                                      bridge=bridge)
            except Unanswered:
                sock.close()
                raise PunchError(f"the host did not answer through {relay['host']}") from None
            except PunchError:
                sock.close()
                raise
            except Exception as exc:
                sock.close()
                raise PunchError(f"the host did not answer through {relay['host']} ({type(exc).__name__})") from exc
            log.info("Reached the host through %s at %s", what, relay["host"])
            return client
        except (PunchError, relayproto.RelayError) as exc:
            bridge.close()
            log.info("Through %s at %s: %s", what, relay["host"], exc)
            if "certificate differs" in str(exc):
                raise
            why.append(str(exc))
    raise PunchError("; ".join(why) or "no relay to try")


def _first_tunnel(attempts, fingerprint):
    """Try every (kind, sock, remote, seconds) at once. Returns (the first tunnel that opens or None, {kind: the
    exception that ended each other one}). Attempts still running when one wins are closed when they end."""
    lock, done = threading.Lock(), threading.Event()
    state = {"client": None, "left": len(attempts)}
    why = {}

    def attempt(kind, sock, remote, seconds):
        client = None
        try:
            client = TunnelClient(sock, remote, fingerprint, connect_seconds=seconds)
            client.kind = kind
        except Exception as exc:
            why[kind] = exc
            log.info("Over %s: %s", "IPv6" if kind == "ipv6" else "IPv4", _attempt_words(exc))
        with lock:
            if client is not None and state["client"] is None:
                state["client"], client = client, None
            state["left"] -= 1
            fatal = isinstance(why.get(kind), PunchError) and not isinstance(why.get(kind), Unanswered)
            if state["client"] is not None or state["left"] == 0 or fatal:
                done.set()
        if client is not None:
            client.close()  # the other way won first

    for kind, sock, remote, seconds in attempts:
        threading.Thread(target=attempt, args=(kind, sock, remote, seconds), name=f"privatepartynet-punch-{kind}",
                         daemon=True).start()
    done.wait()
    with lock:
        return state["client"], dict(why)


def _attempt_words(exc):
    if isinstance(exc, Unanswered):
        return (f"{exc}, though the host's probes arrived" if exc.heard else
                f"{exc}; nothing came back from the host at all")
    if isinstance(exc, PunchError):
        return str(exc)
    return f"it failed ({type(exc).__name__}: {exc})"


def _join_punched(host_key, server_id, secret, fingerprint, dht_factory, stun_servers, connect_seconds, find_seconds,
                  quick=False, ipv6=None):
    """`quick` (there is a relay to fall back on) gives up early where punching rarely works: at once when the host's
    router gives a new port for every destination (the joiner can only aim at the port STUN saw, and the host answers
    from another), and after QUICK_SECONDS when only the joiner's does. IPv6 and IPv4 are tried at the same time."""
    from privatepartynet.dht import Dht

    facts = {"host_found": False}
    dht = dht_factory() if dht_factory else Dht()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock6 = None
    try:
        host = host6 = found = None
        log.info("Looking for the host on the DHT (up to %.0f seconds)", find_seconds)
        looking_until = time.monotonic() + find_seconds
        while host is None and host6 is None and time.monotonic() < looking_until:
            # The host republishes every minute; a lookup that misses (a record still being stored, a slow corner of
            # the DHT) is tried again until find_seconds have passed.
            found = dht.get_mutable(bytes.fromhex(host_key), host_salt(server_id))
            host = read_record(found[1]) if found else None
            host6 = read_record6(found[1]) if found else None
            if host is None and host6 is None:
                time.sleep(2)
        if host is None and host6 is None:
            log.info("The host was not found on the DHT")
            raise PunchError(f"the host's PC was not found on the DHT (the shared lookup network the invite uses) "
                             f"within {find_seconds:.0f} seconds: their party may be closed, or their PC cannot reach "
                             f"the DHT", facts)
        facts.update(host_found=True, host_version=record_version(found[1]), host_v6=host6 is not None,
                     host_nat=host[2] if host else None)
        log.info("Found the host on the DHT (PrivatePartyNet %s). IPv4: %s. IPv6: %s",
                 facts["host_version"] or "3.1 or older",
                 f"{host[0]} port {host[1]}, router {_nat_words(host[2])}" if host else "none",
                 f"{host6[0]} port {host6[1]}" if host6 else "none (the host has no global IPv6 address, or runs 3.0)")
        my6 = None
        addresses = list(ipv6()) if ipv6 is not None else []
        if host6 is not None and addresses:
            try:
                sock6 = _v6_socket()
                my6 = (addresses[0], sock6.getsockname()[1])
            except OSError:
                log.info("Could not open an IPv6 socket", exc_info=True)
                sock6 = None
        facts["my_v6"] = bool(addresses)
        log.info("This PC's IPv6: %s", f"{my6[0]} port {my6[1]}" if my6 else
                 (addresses[0] + ", not used: the host has none" if addresses else "none"))
        try:
            mine = learn_mapping(sock, stun_servers) if host is not None else None
        except PunchError as exc:
            log.info("This PC's IPv4 as the internet sees it: unknown (%s)", exc)
            if my6 is None:
                raise PunchError(str(exc), facts) from None
            mine = None  # no IPv4 to punch with, but IPv6 may do
        if mine is not None:
            facts["my_nat"] = mine[2]
            log.info("This PC's IPv4 as the internet sees it: %s port %s, router %s", mine[0], mine[1],
                     _nat_words(mine[2]))
        stored = dht.put_mutable(mailbox_key(secret), record(*(mine or (None, 0, "")), v6=my6), int(time.time()),
                                 GUEST_SALT)
        facts["mailbox_stored"] = stored
        log.info("Wrote this PC's addresses to the invite's mailbox on the DHT (stored on %d nodes); waiting up to %.0f "
                 "seconds for the host to notice and open its side", stored, connect_seconds)
    except Exception:
        dht.close()
        sock.close()
        if sock6 is not None:
            sock6.close()
        raise
    stop = threading.Event()

    def keep_writing():
        # The host reads mailboxes every few seconds, one DHT lookup each; writing again every 20 seconds (with a newer
        # seq) replaces a record lost on a slow node.
        while not stop.wait(20.0):
            try:
                dht.put_mutable(mailbox_key(secret), record(*(mine or (None, 0, "")), v6=my6), int(time.time()),
                                GUEST_SALT)
            except Exception:
                pass

    writer = threading.Thread(target=keep_writing, name="privatepartynet-punch-mailbox", daemon=True)
    writer.start()
    try:
        attempts = []
        if my6 is not None:
            attempts.append(("ipv6", sock6, host6, connect_seconds))
        v4_why = None
        if host is None:
            v4_why = "the host has no IPv4 address in its record"
        elif mine is None:
            v4_why = "this PC's public IPv4 address is not known (no STUN server answered)"
        elif host[2] == "hard" and mine[2] == "hard":
            v4_why = BOTH_HARD if not quick else BOTH_HARD.split(". Set up")[0]  # with a relay, that comes next
        elif quick and host[2] == "hard":
            v4_why = "the host's router gives a new port for every destination"
        if v4_why:
            log.info("Not trying IPv4: %s", v4_why)
            sock.close()
        else:
            seconds = min(connect_seconds, QUICK_SECONDS) if quick and mine[2] == "hard" else connect_seconds
            attempts.append(("punch", sock, host[:2], seconds))
        if not attempts:
            raise PunchError(v4_why, facts)
        client, why = _first_tunnel(attempts, fingerprint)
        if client is not None:
            log.info("Reached the host over %s", "IPv6" if client.kind == "ipv6" else "IPv4 through both routers")
            return client
        for exc in why.values():
            if isinstance(exc, PunchError) and not isinstance(exc, Unanswered):
                raise PunchError(str(exc), facts)  # a certificate that differs
        facts["host_heard"] = any(getattr(exc, "heard", False) for exc in why.values())
        if facts["host_heard"]:
            reason = (f"the host opened its side, but the connection did not finish within {connect_seconds:.0f} "
                      f"seconds")
        else:
            reason = (f"the host's PC did not answer within {connect_seconds:.0f} seconds: it never noticed this "
                      f"request, or its routers let nothing through")
        if v4_why == BOTH_HARD:
            reason += "; and " + BOTH_HARD
        raise PunchError(reason, facts)
    finally:
        stop.set()
        writer.join(timeout=30)
        dht.close()
