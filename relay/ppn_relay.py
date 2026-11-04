"""PrivatePartyNet relay server, for a group that runs its own (see README.md in this folder).

It forwards datagrams between two members who cannot reach each other: over UDP, and over TLS on TCP port 443 for a
network that lets no UDP out. Everything it forwards is already encrypted end to end (WireGuard, QUIC), so it cannot
read or change what passes; it only sees which two addresses use a channel, how much, and when. It keeps nothing on
disk. Members prove they may use it with tokens made from the relay secret, which only the group's owner holds; each
end of a channel has its own rate limit.

    python3 ppn_relay.py --secret-file /etc/ppn-relay/secret [--udp-port 3479] [--tls-port 443 --cert C --key K]

Standard library only. Python 3.12, as on Ubuntu 24.04 LTS, is what it is made and tested for: from 3.11 on, a TLS
connection counts against the limits from the moment it opens, its handshake included. The rules live in
privatepartynet/relayproto.py, the same file the app uses. Send it SIGHUP after changing the secret file.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import socket
import ssl
import struct
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent):
    if (candidate / "privatepartynet" / "relayproto.py").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from privatepartynet import relayproto as rp  # noqa: E402

log = logging.getLogger("ppn-relay")
LENGTH = struct.Struct("!H")
TCP_BUFFER = 64 * 1024  # bytes waiting for one TCP member before more for them is dropped
# TLS connections in all. Each costs about 320-400 KiB at worst (asyncio's TLS layer keeps a 256 KiB buffer per
# connection, plus TCP_BUFFER of output; measured with verify_memory.py), so 512 of them stay near 200 MiB, well inside
# the service's MemoryMax of 512M.
MAX_TCP = 512
MAX_TCP_PER_ADDRESS = 8
HANDSHAKE_SECONDS = 10.0  # for the TLS handshake, for each frame once its length has arrived, and for a first BIND
STATS_SECONDS = 300


class RelayServer:
    """The relay on one asyncio loop. start() opens the ports; close() ends it."""

    def __init__(self, secrets_, host="::", udp_port=rp.DEFAULT_UDP_PORT, tls_port=None, certfile=None,
                 keyfile=None, rate=rp.DEFAULT_RATE, burst=rp.DEFAULT_BURST, user_rate=rp.DEFAULT_USER_RATE,
                 total_rate=rp.DEFAULT_TOTAL_RATE, max_channels=rp.MAX_CHANNELS, handshake_seconds=HANDSHAKE_SECONDS,
                 max_per_address=MAX_TCP_PER_ADDRESS, max_tcp=MAX_TCP):
        self.core = rp.RelayCore([rp.HmacVerifier(secrets_)], rate=rate, burst=burst, user_rate=user_rate,
                                 total_rate=total_rate, max_channels=max_channels)
        self.host, self.udp_port, self.tls_port = host, udp_port, tls_port
        self.certfile, self.keyfile = certfile, keyfile
        self.handshake_seconds, self.max_per_address, self.max_tcp = handshake_seconds, max_per_address, max_tcp
        self.refused_tcp = 0
        self._udp = None
        self._udp_v6 = False
        self._tls = None
        self._context = None
        self._tcp = {}  # connection number -> writer
        self._addresses = {}  # remote address -> TCP connections open from it
        self._next = 0

    def set_secrets(self, secrets_):
        """The owner changed the secret: members on an old one that is gone are dropped at once."""
        self.core.set_verifiers([rp.HmacVerifier(secrets_)])

    async def start(self):
        loop = asyncio.get_running_loop()
        server = self

        class Udp(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                server._handle(data, ("udp", address[:2]))

            def error_received(self, exc):
                pass  # a member's router said no to one datagram; the next BIND sorts it out

        dual = self.host in ("::", "")
        sock = _dual_stack_udp(self.udp_port) if dual else None
        if sock is not None:
            self._udp, _ = await loop.create_datagram_endpoint(Udp, sock=sock)
        else:
            self._udp, _ = await loop.create_datagram_endpoint(
                Udp, local_addr=("0.0.0.0" if dual else self.host, self.udp_port))
        self.udp_port = self._udp.get_extra_info("sockname")[1]
        self._udp_v6 = len(self._udp.get_extra_info("sockname")) == 4
        tcp_host = None if dual else self.host  # None: every address of both families
        if self.tls_port is not None and self.certfile:
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(self.certfile, self.keyfile)
            if hasattr(asyncio.StreamWriter, "start_tls"):
                # Python 3.11 and later: TLS starts inside the handler, so every connection counts from the moment it is
                # accepted, the handshake included.
                self._tls = await asyncio.start_server(self._serve_tcp, tcp_host, self.tls_port)
            else:
                self._tls = await asyncio.start_server(self._serve_tcp, tcp_host, self.tls_port, ssl=self._context,
                                                       ssl_handshake_timeout=self.handshake_seconds)
            self.tls_port = self._tls.sockets[0].getsockname()[1]

    def _handle(self, data, end):
        for (kind, where), message in self.core.handle(data, end, routable=end[0] == "tcp"):
            if kind == "udp":
                # An IPv6 socket wants the address in full (Windows refuses two parts); an IPv4 member on it shows as
                # ::ffff:a.b.c.d.
                self._udp.sendto(message, (*where, 0, 0) if self._udp_v6 else where)
                continue
            writer = self._tcp.get(where)
            if writer is not None and writer.transport.get_write_buffer_size() < TCP_BUFFER:
                writer.write(LENGTH.pack(len(message)) + message)

    async def _serve_tcp(self, reader, writer):
        address = (writer.get_extra_info("peername") or ("?",))[0]
        if len(self._tcp) >= self.max_tcp or self._addresses.get(address, 0) >= self.max_per_address:
            self.refused_tcp += 1
            writer.close()
            return
        self._next += 1
        number, end = self._next, ("tcp", self._next)
        self._tcp[number] = writer
        self._addresses[address] = self._addresses.get(address, 0) + 1
        loop = asyncio.get_running_loop()
        try:
            if writer.get_extra_info("sslcontext") is None:
                await asyncio.wait_for(writer.start_tls(self._context), self.handshake_seconds)
            # A connection must bind within a few seconds, and stays only while it holds a channel.
            unbound_until = loop.time() + self.handshake_seconds
            while True:
                if self.core.has_end(end):
                    unbound_until = loop.time() + self.handshake_seconds
                    wait = rp.IDLE_SECONDS
                else:
                    wait = unbound_until - loop.time()
                    if wait <= 0:
                        break
                head = await asyncio.wait_for(reader.readexactly(2), wait)
                size = LENGTH.unpack(head)[0]
                if not rp.HEADER <= size <= rp.MAX_MESSAGE:
                    break
                body = await asyncio.wait_for(reader.readexactly(size), self.handshake_seconds)
                self._handle(body, end)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            self._tcp.pop(number, None)
            left = self._addresses.get(address, 1) - 1
            if left:
                self._addresses[address] = left
            else:
                self._addresses.pop(address, None)
            self.core.drop_end(end)
            writer.close()

    async def report(self, every=STATS_SECONDS):
        while True:
            await asyncio.sleep(every)
            self.core.sweep()
            log.info("channels %d, forwarded %d datagrams (%d bytes), limited %d, refused %d",
                     len(self.core.channels), self.core.stats["forwarded"], self.core.stats["bytes"],
                     self.core.stats["limited"], self.core.stats["refused"])

    def close(self):
        if self._udp is not None:
            self._udp.close()
        if self._tls is not None:
            self._tls.close()
        for writer in list(self._tcp.values()):
            writer.close()


def _dual_stack_udp(port):
    """One UDP socket for IPv6 and IPv4 together, so a member on either reaches the same relay (and two members on
    different families meet through it). None where the system has no IPv6; then IPv4 alone."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    except OSError:
        sock.close()
        return None
    return sock


class ThreadedRelay:
    """A RelayServer on a thread of its own, for tests and for running beside something else."""

    def __init__(self, secrets_, **options):
        self.loop = asyncio.new_event_loop()
        self.server = RelayServer(secrets_, **options)
        self._thread = threading.Thread(target=self.loop.run_forever, name="ppn-relay", daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(10)

    @property
    def udp_port(self):
        return self.server.udp_port

    @property
    def tls_port(self):
        return self.server.tls_port

    def close(self):
        async def finish():
            self.server.close()
            await asyncio.sleep(0.05)  # the transports close on the loop's next turns

        try:
            asyncio.run_coroutine_threadsafe(finish(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)
            if not self.loop.is_running():
                self.loop.close()


def certificate_pin(certfile):
    """The SHA-256 of the certificate, in hex: what the owner puts in the app beside the address."""
    text = Path(certfile).read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(text)).hexdigest()


def read_secrets(arguments):
    found = []
    if arguments.secret_file:
        found += [line.strip() for line in Path(arguments.secret_file).read_text(encoding="utf-8").splitlines()]
    if os.environ.get("PPN_RELAY_SECRET"):
        found.append(os.environ["PPN_RELAY_SECRET"].strip())
    # More than one line: the old secret keeps working while members get tokens made from the new one.
    return [s for s in found if s and not s.startswith("#")]


def main(argv=None):
    parser = argparse.ArgumentParser(description="PrivatePartyNet relay server")
    parser.add_argument("--secret-file", help="file with the relay secret (one per line; the first is current)")
    parser.add_argument("--host", default="::", help="address to listen on (the default, ::, is every address of IPv6 "
                                                      "and IPv4)")
    parser.add_argument("--udp-port", type=int, default=rp.DEFAULT_UDP_PORT)
    parser.add_argument("--tls-port", type=int, default=rp.DEFAULT_TLS_PORT, help="0 turns the TLS fallback off")
    parser.add_argument("--cert", help="TLS certificate (PEM) for the fallback")
    parser.add_argument("--key", help="its private key (PEM)")
    parser.add_argument("--rate-kbps", type=int, default=rp.DEFAULT_RATE * 8 // 1000,
                        help="the most each member sends through one channel, in kilobits a second")
    parser.add_argument("--user-kbps", type=int, default=rp.DEFAULT_USER_RATE * 8 // 1000,
                        help="the most each member sends through all their channels together")
    parser.add_argument("--total-mbps", type=int, default=rp.DEFAULT_TOTAL_RATE * 8 // 1_000_000,
                        help="the most the relay forwards for everyone together, in megabits a second (0: no limit)")
    parser.add_argument("--max-channels", type=int, default=rp.MAX_CHANNELS)
    parser.add_argument("--max-tcp", type=int, default=MAX_TCP, help="TLS connections at once, from everyone together")
    parser.add_argument("--new-secret", action="store_true", help="print a new random secret and stop")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if arguments.new_secret:
        print(rp.new_secret())
        return 0
    if sys.version_info < (3, 12):
        log.warning("Python %d.%d: the relay is made for 3.12 (Ubuntu 24.04). Before 3.11, connections still in their "
                    "TLS handshake do not count against the connection limits.", *sys.version_info[:2])
    secrets_ = read_secrets(arguments)
    if not secrets_:
        parser.error("no secret: give --secret-file or set PPN_RELAY_SECRET (make one with --new-secret)")
    tls = arguments.tls_port if arguments.tls_port and arguments.cert and arguments.key else None
    rate = max(8, arguments.rate_kbps) * 1000 // 8
    server = RelayServer(secrets_, host=arguments.host, udp_port=arguments.udp_port, tls_port=tls,
                         certfile=arguments.cert, keyfile=arguments.key, rate=rate,
                         burst=max(rp.DEFAULT_BURST, rate // 4), user_rate=max(8, arguments.user_kbps) * 1000 // 8,
                         total_rate=arguments.total_mbps * 125_000 or None, max_channels=arguments.max_channels,
                         max_tcp=arguments.max_tcp)

    async def run():
        await server.start()
        log.info("Relaying on UDP %s", server.udp_port)
        if tls:
            log.info("TLS fallback on TCP %s, certificate pin %s", server.tls_port, certificate_pin(arguments.cert))
        else:
            log.info("No TLS fallback (give --cert and --key to have one)")
        stop = asyncio.Event()

        def reload():
            fresh = read_secrets(arguments)
            if fresh:
                server.set_secrets(fresh)
                log.info("Secrets read again: %d in use", len(fresh))

        handlers = {"SIGINT": stop.set, "SIGTERM": stop.set, "SIGHUP": reload}
        for name, handler in handlers.items():
            if hasattr(signal, name):
                try:
                    asyncio.get_running_loop().add_signal_handler(getattr(signal, name), handler)
                except NotImplementedError:
                    pass
        reporter = asyncio.ensure_future(server.report())
        await stop.wait()
        reporter.cancel()
        server.close()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
