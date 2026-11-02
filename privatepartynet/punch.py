"""Reaching a party whose host lets nothing in (carrier-grade NAT, no IPv6, no open port), with no relay and no server.

Both PCs make one UDP socket each, learn the public address their router gives that socket (STUN), and swap those
addresses through signed records on the BitTorrent DHT (dht.py): the host publishes its own under its identity key, and a
joiner writes its own into a "mailbox" whose key both sides derive from a secret in the invite. Then both send small
probes to each other's public address at the same time, which opens each router for the other's packets (hole punching).
Over that same socket a QUIC connection (aioquic) carries the party's own TLS connections, one QUIC stream each, so the
pages, the join and the game network work exactly as on a LAN. The joiner sees them on a loopback port.

What it cannot do: two routers that each give a new public port for every destination ("hard" NAT on both sides). The
STUN check says which kind a router is, and the app says so plainly instead of trying for ever. Nothing relays traffic.

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

from privatepartynet.connect import STUN_SERVERS, _stun_binding

log = logging.getLogger("privatepartynet.punch")

ALPN = "privatepartynet-tunnel/1"
PROBE = b"PRIVATEPARTYNET-PUNCH/1"
PROBE_SECONDS = 0.2
HOST_SALT = b"privatepartynet-host"
GUEST_SALT = b"privatepartynet-guest"
TUNNEL_NETWORK = ipaddress.ip_network("127.1.0.0/16")  # where tunnelled connections come from, as the party sees them
CONNECT_SECONDS = 30.0
IDLE_SECONDS = 60.0


class PunchError(Exception):
    pass


# --- the rendezvous records -----------------------------------------------------------------------------------------


def mailbox_key(secret):
    """The key both sides derive from the invite's secret: whoever holds the invite may write their address there."""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"PrivatePartyNet mailbox|" + bytes(secret)).digest())


def host_salt(server_id):
    return HOST_SALT + b"|" + str(server_id).encode("ascii", "replace")[:40]


def record(address, port, nat):
    return {"a": address, "p": int(port), "n": nat, "t": int(time.time())}


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
        except (ConnectionError, OSError, RuntimeError):
            pass


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
                self.transport.sendto(PROBE, remote)
                await asyncio.sleep(PROBE_SECONDS)

        asyncio.run_coroutine_threadsafe(send(), self._loop.loop)

    def close(self):
        self._loop.stop(closing=(self.server, self.transport))


# --- the joiner's side ----------------------------------------------------------------------------------------------


class TunnelClient:
    """QUIC from one punched socket to the host, pinned to the host certificate's fingerprint. Connections to the
    loopback port `port` are carried to the party, one QUIC stream each."""

    def __init__(self, sock, remote, fingerprint, connect_seconds=CONNECT_SECONDS):
        from aioquic.asyncio.protocol import QuicConnectionProtocol
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection

        self.sock = sock
        self.remote = (remote[0], int(remote[1]))
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

            seen = hashlib.sha256(certificate.public_bytes(Encoding.DER)).hexdigest() if certificate else ""
            if seen != fingerprint:
                protocol.close()
                raise PunchError("the host at that address is not the one this invite is for (its certificate differs)")

            async def carry(local_reader, local_writer):
                reader, writer = await protocol.create_stream()
                await asyncio.gather(_pipe(local_reader, writer), _pipe(reader, local_writer))

            listener = await asyncio.start_server(carry, "127.0.0.1", 0)
            return transport, protocol, listener

        try:
            self.transport, self.protocol, self._listener = self._loop.run(start(), connect_seconds + 5)
        except Exception:
            self._loop.stop()
            raise
        self.port = self._listener.sockets[0].getsockname()[1]

    def close(self):
        self._loop.stop(closing=(self._listener, self.protocol, self.transport))


# --- the whole rendezvous -------------------------------------------------------------------------------------------


class HostPunch:
    """While a party is open: keep a punched socket with a known public address, publish it on the DHT, watch the
    mailboxes of the invites given out, and open the way for each joiner who writes there."""

    def __init__(self, identity_key, server_id, certfile, keyfile, party_port, secrets_, dht_factory=None,
                 stun_servers=STUN_SERVERS, refresh=60.0, poll=5.0, max_seconds=24 * 3600):
        self._key = identity_key
        self._server_id = server_id
        self._secrets = secrets_  # a callable: the invite secrets still valid
        self._dht_factory = dht_factory
        self._stun = stun_servers
        self._refresh, self._poll, self._max_seconds = refresh, poll, max_seconds
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.mapping = learn_mapping(self.sock, stun_servers)
        self.tunnel = TunnelHost(self.sock, certfile, keyfile, party_port)
        self.seen = {}  # joiner's public address -> time it was last opened for
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
                address, port, nat = self.mapping
                try:
                    dht.put_mutable(self._key, record(address, port, nat), seq, host_salt(self._server_id))
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
                self._stop.wait(self._poll)
        finally:
            dht.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=30)
        self.tunnel.close()
        try:
            self.sock.close()
        except OSError:
            pass


def join_through(host_key, server_id, secret, fingerprint, dht_factory=None, stun_servers=STUN_SERVERS,
                 connect_seconds=CONNECT_SECONDS, find_seconds=60.0):
    """From a joiner's PC: find the host's public address on the DHT, write ours into the invite's mailbox, punch, and
    open the QUIC tunnel. Returns a TunnelClient whose .port is the loopback port to reach the party on."""
    from privatepartynet.dht import Dht

    dht = dht_factory() if dht_factory else Dht()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    try:
        host = None
        looking_until = time.monotonic() + find_seconds
        while host is None and time.monotonic() < looking_until:
            # The host republishes every minute; a lookup that misses (a record still being stored, a slow corner of
            # the DHT) is tried again until find_seconds have passed.
            found = dht.get_mutable(bytes.fromhex(host_key), host_salt(server_id))
            host = read_record(found[1]) if found else None
            if host is None:
                time.sleep(2)
        if host is None:
            raise PunchError("the host's address was not found on the network; is their party open?")
        mine = learn_mapping(sock, stun_servers)
        if host[2] == "hard" and mine[2] == "hard":
            raise PunchError("both routers give a new port for every destination, which no direct connection can "
                             "cross. IPv6, or a forwarded port on either side, would work.")
        dht.put_mutable(mailbox_key(secret), record(*mine), int(time.time()), GUEST_SALT)
    except Exception:
        dht.close()
        sock.close()
        raise
    stop = threading.Event()

    def keep_writing():
        # The host reads mailboxes one lookup at a time; writing again every 20 seconds (with a newer seq) means the
        # host acts on it as soon as it next looks, and a record lost on a slow node is replaced.
        while not stop.wait(20.0):
            try:
                dht.put_mutable(mailbox_key(secret), record(*mine), int(time.time()), GUEST_SALT)
            except Exception:
                pass

    writer = threading.Thread(target=keep_writing, name="privatepartynet-punch-mailbox", daemon=True)
    writer.start()
    try:
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
