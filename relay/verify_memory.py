"""How much memory the relay needs at its worst, measured on the machine it runs on. Standard library only.

    python3 verify_memory.py [--members 512] [--limit-mib 512]

It starts a relay of its own on loopback (with a certificate made by openssl), fills it with TLS members that bind a
channel and then never read while the other end of each channel floods them over UDP, so every output buffer fills,
and opens more connections than the relay allows. It checks that the extra ones are closed at once and that the
relay's memory stays under the limit, then prints what one member costs. Run it on the VPS after setup-ubuntu.sh, with
the relay service left running: it uses ports of its own. It needs about 600 file descriptors (ulimit -n).

Each member needs a source address of its own now and then, since the relay takes at most eight connections from one
address; it uses 127.0.x.y, which Linux and Windows route to loopback.
"""

import argparse
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent):
    if (candidate / "privatepartynet" / "relayproto.py").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from privatepartynet import relayproto as rp  # noqa: E402

SECRET = "verify-memory-secret-0123456789"
PER_ADDRESS = 8  # keep in step with MAX_TCP_PER_ADDRESS in ppn_relay.py


def rss(pid):
    """Resident memory of a process in bytes: /proc on Linux, psutil elsewhere when it is there, else None."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    try:
        import psutil

        process = psutil.Process(pid)
        children = process.children(recursive=True)  # a venv launcher on Windows starts the real interpreter
        return (children[0] if children else process).memory_info().rss
    except Exception:
        return None


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def certificate(folder):
    cert, key = os.path.join(folder, "relay.crt"), os.path.join(folder, "relay.key")
    openssl = shutil.which("openssl")
    if openssl:
        subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-days", "1", "-subj", "/CN=verify", "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        return cert, key
    from privatepartynet.cert import ensure_certificate  # the app's own, where the app is installed (cryptography)

    return tuple(str(p) for p in ensure_certificate(folder, ["localhost"]))


def closed_at_once(sock, seconds=3.0):
    sock.settimeout(seconds)
    try:
        return sock.recv(1) == b""
    except (ConnectionError, ssl.SSLError):
        return True
    except (socket.timeout, OSError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure the relay's memory at its worst")
    parser.add_argument("--members", type=int, default=512, help="TLS members to fill it with (its MAX_TCP)")
    parser.add_argument("--extra", type=int, default=16, help="connections beyond that, which must be refused")
    parser.add_argument("--limit-mib", type=int, default=512, help="the memory it must stay under (MemoryMax)")
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to flood")
    arguments = parser.parse_args(argv)

    folder = tempfile.mkdtemp(prefix="ppn-verify-")
    secret_file = os.path.join(folder, "secret")
    Path(secret_file).write_text(SECRET + "\n", encoding="utf-8")
    cert, key = certificate(folder)
    udp_port, tls_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_STREAM)
    relay = subprocess.Popen([sys.executable, str(HERE / "ppn_relay.py"), "--secret-file", secret_file, "--host",
                              "127.0.0.1", "--udp-port", str(udp_port), "--tls-port", str(tls_port), "--cert", cert,
                              "--key", key, "--total-mbps", "0", "--max-tcp", str(arguments.members)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sockets = []
    try:
        time.sleep(2)
        base = rss(relay.pid)
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        floods = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(8)]
        for udp in floods:
            udp.bind(("127.0.0.1", 0))
            udp.settimeout(2)
            sockets.append(udp)
        channels = []
        for number in range(arguments.members):
            source = f"127.0.{10 + number // PER_ADDRESS}.{1 + number % PER_ADDRESS}"
            raw = socket.create_connection(("127.0.0.1", tls_port), timeout=5, source_address=(source, 0))
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            member = context.wrap_socket(raw)
            sockets.append(member)
            channel = os.urandom(16)
            message = rp.bind(channel, rp.member_credential(SECRET, os.urandom(16)))
            member.sendall(struct.pack("!H", len(message)) + message)
            member.settimeout(5)
            member.recv(64)  # BOUND; from now on this member never reads
            udp = floods[number % len(floods)]
            credential = rp.member_credential(SECRET, os.urandom(16))
            udp.sendto(rp.bind(channel, credential), ("127.0.0.1", udp_port))
            cookie = rp.parse(udp.recv(100))[2][1:]
            udp.sendto(rp.bind(channel, credential, cookie), ("127.0.0.1", udp_port))
            udp.recv(100)
            channels.append((udp, channel))
        members_rss = rss(relay.pid)
        refused = 0
        for number in range(arguments.extra):
            source = f"127.0.{200 + number // PER_ADDRESS}.{1 + number % PER_ADDRESS}"
            extra = socket.create_connection(("127.0.0.1", tls_port), timeout=5, source_address=(source, 0))
            sockets.append(extra)
            refused += closed_at_once(extra)
        payload = b"x" * rp.MAX_PAYLOAD
        ends = time.monotonic() + arguments.seconds
        while time.monotonic() < ends:
            for udp, channel in channels:
                try:
                    udp.sendto(rp.send(channel, payload), ("127.0.0.1", udp_port))
                except OSError:
                    pass
            time.sleep(0.01)
        peak = rss(relay.pid)
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        relay.kill()
        shutil.rmtree(folder, ignore_errors=True)

    mib = lambda value: f"{value / 2 ** 20:.1f} MiB" if value else "unknown"  # noqa: E731
    print(f"relay at rest: {mib(base)}; with {arguments.members} TLS members: {mib(members_rss)}; "
          f"with their buffers full: {mib(peak)}")
    if base and peak:
        print(f"about {(peak - base) / arguments.members / 1024:.0f} KiB a member")
    print(f"{refused} of {arguments.extra} connections beyond the limit were closed at once")
    ok = refused == arguments.extra and (peak is None or peak < arguments.limit_mib * 2 ** 20)
    if peak is None:
        print("Memory could not be read on this system (no /proc, no psutil); only the refusals were checked.")
    print("PASS" if ok else f"FAIL: over {arguments.limit_mib} MiB or a connection beyond the limit was kept")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
