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
CONNECT_SECONDS = 30.0
V6_SECONDS = 8.0  # how long the join tries IPv6 before IPv4, when IPv4 is there to try too
QUICK_SECONDS = 6.0  # how long to try punching from behind a hard router before taking a relay instead
IDLE_SECONDS = 60.0


class PunchError(Exception):
    pass


# --- the rendezvous records -----------------------------------------------------------------------------------------


def mailbox_key(secret):
    """The key both sides derive from the invite's secret: whoever holds the invite may write their address there."""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"PrivatePartyNet mailbox|" + bytes(secret)).digest())


def host_salt(server_id):
    return HOST_SALT + b"|" + str(server_id).encode("ascii", "replace")[:40]


def record(address, port, nat, v6=None):
    """A DHT record: the IPv4 address and port STUN saw (address None when there is none), and `v6`, an IPv6
    (address, port), when there is one."""
    value = {"t": int(time.time())}
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
        remote = writer.get_extra_info("peername") or ("0.0.0.0", 0)
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
    """QUIC from one punched socket to the host, pinned to the host certificate (a pin or a list of pins: the whole
    certificate's SHA-256 or its public key's, cert.matches). Connections to the
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
        # The idle limit must outlast the wait for the host to open its router, or QUIC gives up first (seen: 60 s).
        configuration = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                          idle_timeout=max(IDLE_SECONDS, connect_seconds + 15),
                                          verify_mode=ssl.CERT_NONE, server_name="privatepartynet")

        async def start():
            loop = asyncio.get_running_loop()
            connection = QuicConnection(configuration=configuration)
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: QuicConnectionProtocol(connection), sock=sock)
            # Keep sending probes until the handshake is through: they open this router for the host's answers.
            probing = True

            async def probe():
                while probing:
                    transport.sendto(PROBE, self.remote)
                    await asyncio.sleep(PROBE_SECONDS)

            prober = asyncio.ensure_future(probe())
            try:
                protocol.connect(self.remote)
                await asyncio.wait_for(protocol.wait_connected(), connect_seconds)
            finally:
                probing = False
                prober.cancel()
            certificate = connection.tls._peer_certificate
            from cryptography.hazmat.primitives.serialization import Encoding

            from privatepartynet.cert import matches

            if not matches(fingerprint, certificate.public_bytes(Encoding.DER) if certificate else b""):
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
        except Exception:
            self._loop.stop()
            try:
                sock.close()  # the transport owned it, but never closed it on the way out
            except OSError:
                pass
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
                 stun_servers=STUN_SERVERS, refresh=60.0, poll=5.0, max_seconds=24 * 3600, relays=None, ipv6=None):
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
                        dht.put_mutable(self._key, record(address, port, nat, self.mapping6), seq,
                                        host_salt(self._server_id))
                    except Exception:
                        log.debug("Publishing the host record failed", exc_info=True)
                self._stop.wait(self._refresh)
        finally:
            dht.close()
            watcher.join(timeout=30)

    def _watch(self):
        started = time.monotonic()
        dht = self._dht()
        seqs = {}  # mailbox -> the newest seq acted on
        try:
            while not self._stop.is_set() and time.monotonic() - started < self._max_seconds:
                self._sync_relays()
                for secret in list(self._secrets()):
                    if self._stop.is_set():
                        break
                    mailbox = mailbox_key(secret).public_key().public_bytes_raw()
                    try:
                        found = dht.get_mutable(mailbox, GUEST_SALT)
                    except Exception:
                        found = None
                    if not found or found[0] <= seqs.get(mailbox, 0):
                        continue
                    seqs[mailbox] = found[0]
                    joiner = read_record(found[1])
                    if joiner:
                        self.seen[joiner[:2]] = time.monotonic()
                        self.tunnel.probe(joiner[:2], seconds=CONNECT_SECONDS)
                    joiner6 = read_record6(found[1])
                    if joiner6 and self.tunnel6 is not None:
                        # Opens this PC's IPv6 firewall for the joiner, as the probes on IPv4 open its router.
                        self.seen[joiner6] = time.monotonic()
                        self.tunnel6.probe(joiner6, seconds=CONNECT_SECONDS)
                self._stop.wait(self._poll)
        finally:
            dht.close()

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


def join_through(host_key, server_id, secret, fingerprint, dht_factory=None, stun_servers=STUN_SERVERS,
                 connect_seconds=CONNECT_SECONDS, find_seconds=60.0, relays=(), ipv6=None):
    """From a joiner's PC: find the host's public address on the DHT, write ours into the invite's mailbox, punch, and
    open the QUIC tunnel; when that cannot work, open it through one of `relays` (from the invite) instead. Returns a
    TunnelClient whose .port is the loopback port to reach the party on, and whose .kind says which way it went.
    `ipv6` is a callable giving this PC's global IPv6 addresses; with one, and one in the host's record, IPv6 is tried
    first."""
    if not (host_key and server_id):
        if not relays:
            raise PunchError("the invite has nothing to find the host by")
        return join_relayed(secret, fingerprint, relays, connect_seconds)
    try:
        return _join_punched(host_key, server_id, secret, fingerprint, dht_factory, stun_servers, connect_seconds,
                             find_seconds, quick=bool(relays), ipv6=ipv6)
    except PunchError as exc:
        if not relays:
            raise
        log.info("Punching through to the host did not work (%s); trying the relays", exc)
        try:
            return join_relayed(secret, fingerprint, relays, connect_seconds)
        except PunchError as relayed:
            raise PunchError(f"{exc}; and through the relays: {relayed}") from None


def join_relayed(secret, fingerprint, relays, connect_seconds=CONNECT_SECONDS, bind_seconds=8.0):
    """The QUIC tunnel through the first relay that works, in the order given. Each relay is as an invite carries it
    (connect.decode_invite): {"kind", "host", "port", "tls_port", "pin", "token": bytes}. The host's certificate is
    checked exactly as on a direct path."""
    from privatepartynet.pathway import PassCredential, invite_key
    from privatepartynet.relayclient import RelayBridge

    channel = join_channel(secret)
    why = []
    for relay in relays:
        try:
            if relay.get("kind") == "friend":
                credential = PassCredential(relay["token"], invite_key(secret)[0])
            else:
                credential = relayproto.HmacCredential(relay["token"])
        except (KeyError, ValueError):
            why.append(f"{relay.get('host')}: the invite's credentials for it are damaged")
            continue
        bridge = RelayBridge({**relay, "credential": credential})
        try:
            port = bridge.add(channel)
            if not bridge.wait_bound(channel, bind_seconds):
                raise PunchError(f"{relay['host']} did not answer")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            try:
                return TunnelClient(sock, ("127.0.0.1", port), fingerprint, connect_seconds=connect_seconds, bridge=bridge)
            except PunchError:
                sock.close()
                raise
            except Exception as exc:
                sock.close()
                raise PunchError(f"the host did not answer through {relay['host']} ({type(exc).__name__})") from exc
        except (PunchError, relayproto.RelayError) as exc:
            bridge.close()
            if "certificate differs" in str(exc):
                raise
            why.append(str(exc))
    raise PunchError("; ".join(why) or "no relay to try")


def _join_punched(host_key, server_id, secret, fingerprint, dht_factory, stun_servers, connect_seconds, find_seconds,
                  quick=False, ipv6=None):
    """`quick` (there is a relay to fall back on) gives up early where punching rarely works: at once when the host's
    router gives a new port for every destination (the joiner can only aim at the port STUN saw, and the host answers
    from another), and after QUICK_SECONDS when only the joiner's does."""
    from privatepartynet.dht import Dht

    dht = dht_factory() if dht_factory else Dht()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock6 = None
    try:
        host = host6 = None
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
            raise PunchError("the host's address was not found on the network; is their party open?")
        my6 = None
        if host6 is not None and ipv6 is not None:
            addresses = list(ipv6())
            if addresses:
                try:
                    sock6 = _v6_socket()
                    my6 = (addresses[0], sock6.getsockname()[1])
                except OSError:
                    sock6 = None
        try:
            mine = learn_mapping(sock, stun_servers) if host is not None else None
        except PunchError:
            if my6 is None:
                raise
            mine = None  # no IPv4 to punch with, but IPv6 may do
        dht.put_mutable(mailbox_key(secret), record(*(mine or (None, 0, "")), v6=my6), int(time.time()), GUEST_SALT)
    except Exception:
        dht.close()
        sock.close()
        if sock6 is not None:
            sock6.close()
        raise
    stop = threading.Event()

    def keep_writing():
        # The host reads mailboxes one lookup at a time; writing again every 20 seconds (with a newer seq) means the
        # host acts on it as soon as it next looks, and a record lost on a slow node is replaced.
        while not stop.wait(20.0):
            try:
                dht.put_mutable(mailbox_key(secret), record(*(mine or (None, 0, "")), v6=my6), int(time.time()),
                                GUEST_SALT)
            except Exception:
                pass

    writer = threading.Thread(target=keep_writing, name="privatepartynet-punch-mailbox", daemon=True)
    writer.start()
    try:
        if my6 is not None:
            try:
                client = TunnelClient(sock6, host6, fingerprint, bridge=None,
                                      connect_seconds=V6_SECONDS if mine is not None else connect_seconds)
                client.kind = "ipv6"
                sock.close()
                return client
            except PunchError:
                raise  # a certificate that differs: the same over IPv4
            except Exception:
                log.info("The host did not answer over IPv6; trying IPv4")
                sock6 = None  # TunnelClient closed it
        if mine is None:
            raise PunchError("the host did not answer over IPv6, and there is no IPv4 way to try")
        if host[2] == "hard" and mine[2] == "hard":
            raise PunchError("both routers give a new port for every destination, which no direct connection can "
                             "cross. IPv6, or a forwarded port on either side, would work.")
        if quick and host[2] == "hard":
            raise PunchError("the host's router gives a new port for every destination")
        if quick and mine[2] == "hard":
            connect_seconds = min(connect_seconds, QUICK_SECONDS)
        return TunnelClient(sock, host[:2], fingerprint, connect_seconds=connect_seconds)
    except PunchError:
        sock.close()
        raise
    except Exception as exc:
        sock.close()
        raise PunchError(f"the host did not answer through the routers within {connect_seconds:.0f} seconds "
                         f"({type(exc).__name__})") from exc
    finally:
        stop.set()
        writer.join(timeout=30)
        dht.close()
