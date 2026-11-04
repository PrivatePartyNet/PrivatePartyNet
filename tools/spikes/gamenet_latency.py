"""Measures the game network's round trip on this PC: one member pings another, and the other answers.

Both designs run here end to end, from one member's adapter to the other's, with the adapters in memory (no
administrator rights, no Wintun) and everything on loopback, so what is measured is what each design adds by itself:
  * wireguard: member A's helper (WireGuard seals it) -> A's app -> the path to B (in memory) -> B's app -> B's helper
    (WireGuard opens it) -> B's adapter. Straight from member to member.
  * tls (the design before WireGuard; give --old with a copy of that version's source): member A's helper -> A's app
    -> A's TLS link -> the host's router -> B's TLS link -> B's app -> B's helper -> B's adapter. Always through the
    host.
Each is measured idle and while A sends B a steady stream of full-size packets (a game's bulk traffic), and the times
are printed as percentiles in milliseconds. On a real network the TLS design also crosses the internet twice (to the
host and on), and stalls behind any lost packet; neither shows on loopback, so these numbers favour it.

    py tools\\spikes\\gamenet_latency.py                       (this version)
    git archive 4d4a150 --prefix=old/ | tar -x -C %TEMP%
    py tools\\spikes\\gamenet_latency.py --old %TEMP%\\old     (the version before, run with that source)
"""

import argparse
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PINGS = 400
LOAD_PPS = 2000  # packets a second of bulk traffic during the loaded run (about 2.6 MB/s at 1300 bytes)


class Ends:
    """send_a(packet) puts a packet on A's adapter; A's and B's adapters deliver to the queues a_in and b_in."""

    def __init__(self):
        self.a_in, self.b_in = queue.Queue(), queue.Queue()


def wireguard_ends(folder):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from gamehelper import FakePaths, Hub, helper_program, launch_plain
    from privatepartynet.gameclient import GameMesh, GameSession
    from privatepartynet.gamenet import new_private_key, public_key

    program = helper_program()
    if program is None:
        sys.exit("Go is not installed, so the helper cannot be built")
    hub = Hub()
    ends = Ends()
    members = []
    for user, address, inbox in (("a", "10.77.12.1", ends.a_in), ("b", "10.77.12.2", ends.b_in)):
        private = new_private_key()
        session = GameSession(address, "10.77.12.0/24", private, folder, launch=launch_plain, program=program,
                              memory_adapter=True, on_packet=inbox.put)
        mesh = GameMesh(user, FakePaths(user, hub), session)
        session.start()
        members.append((user, address, public_key(private), session, mesh))
    while any(m[3].status["state"] != "on" for m in members):
        time.sleep(0.05)
    roster = {"peers": [{"user": u, "address": a, "key": k, "candidates": []} for u, a, k, _, _ in members]}
    for member in members:
        member[4].apply(roster)
    ends.send_a, ends.send_b = members[0][3].put_packet, members[1][3].put_packet
    ends.a, ends.b = "10.77.12.1", "10.77.12.2"
    ends.stop = lambda: [m[3].stop() for m in members]
    return ends


def forward(source, target):
    while True:
        target.put(source.get())


def tls_ends(folder, old):
    """The design before WireGuard, from its own source: its helper's serve() on an in-memory adapter, its app session,
    its TLS links and the host's router."""
    sys.path.insert(0, str(old))
    sys.path.insert(0, str(old / "tests"))
    from test_gameclient import FakeLaunch
    from privatepartynet.cert import ensure_certificate
    from privatepartynet.gameclient import GameSession
    from privatepartynet.gamenet import MemoryTun
    from privatepartynet.identity import Identity
    from privatepartynet.netlink import NetLink
    from privatepartynet.server import Party

    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("bench", 0, certfile, keyfile, party_name="Bench", host_name="Host", failed_login_delay=0,
                  server_id="bench0000bench00")
    party.start()
    ends = Ends()
    sessions, links = [], []
    for name, inbox in (("A", ends.a_in), ("B", ends.b_in)):
        identity = Identity.create()
        party.attach(party.member_for(party.redeem(party.join_token(name, user=identity.user_id))))
        tun = MemoryTun()
        holder = {}
        link = NetLink("127.0.0.1", party.port, party.fingerprint, identity,
                       lambda packet, h=holder: h["session"].deliver(packet))
        link.start()
        while link.status["state"] != "connected":
            time.sleep(0.05)
        session = GameSession(link.status["address"], link.status["network"], link.send, folder,
                              launch=FakeLaunch(tun))
        holder["session"] = session
        session.start()
        while session.status["state"] != "on":
            time.sleep(0.05)
        threading.Thread(target=forward, args=(tun.incoming, inbox), daemon=True).start()
        sessions.append((tun, link.status["address"]))
        links.append(link)
    ends.send_a, ends.send_b = sessions[0][0].outgoing.put, sessions[1][0].outgoing.put
    ends.a, ends.b = sessions[0][1], sessions[1][1]

    def stop():
        for link in links:
            link.stop()
        party.stop()

    ends.stop = stop
    return ends


def make_packet(source, destination, payload):
    sys.path.insert(0, str(ROOT))
    from privatepartynet.gamenet import make_packet as make

    return make(source, destination, payload)


def measure(ends, load):
    """Round trips of PINGS pings in milliseconds; B answers each from its own adapter."""
    stop = threading.Event()

    def answer():
        while not stop.is_set():
            try:
                packet = ends.b_in.get(timeout=0.2)
            except queue.Empty:
                continue
            if packet.endswith(b"!ping"):
                ends.send_b(make_packet(ends.b, ends.a, packet[28:]))

    def bulk():
        filler = make_packet(ends.a, ends.b, b"x" * 1272)
        interval = 1.0 / LOAD_PPS
        due = time.perf_counter()
        while not stop.is_set():
            ends.send_a(filler)
            due += interval
            pause = due - time.perf_counter()
            if pause > 0:
                time.sleep(pause)

    threading.Thread(target=answer, daemon=True).start()
    if load:
        threading.Thread(target=bulk, daemon=True).start()
    times = []
    for number in range(PINGS + 20):
        tag = f"{number:08d}".encode() + b"!ping"
        sent = time.perf_counter()
        ends.send_a(make_packet(ends.a, ends.b, b"\0" * 8 + tag))
        deadline = sent + 2.0
        while time.perf_counter() < deadline:
            try:
                packet = ends.a_in.get(timeout=max(0.0, deadline - time.perf_counter()))
            except queue.Empty:
                break
            if packet.endswith(tag):
                if number >= 20:  # the first ones warm up the handshake and the caches
                    times.append((time.perf_counter() - sent) * 1000)
                break
        time.sleep(0.005)
    stop.set()
    return times


def report(name, times):
    if not times:
        print(f"{name:28} no answers")
        return
    cuts = statistics.quantiles(times, n=100)
    print(f"{name:28} median {statistics.median(times):6.2f}  p90 {cuts[89]:6.2f}  p99 {cuts[98]:6.2f}  "
          f"answered {len(times)}/{PINGS}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", help="the source of the version before WireGuard, to measure the TLS design")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="privatepartynet-latency-"))
    name = "tls through the host" if args.old else "wireguard member to member"
    ends = tls_ends(folder, Path(args.old)) if args.old else wireguard_ends(folder)
    time.sleep(1)
    report(name + ", idle", measure(ends, load=False))
    report(name + ", loaded", measure(ends, load=True))
    ends.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
